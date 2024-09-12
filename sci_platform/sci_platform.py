import sys
sys.path.append('../agentscope-main/src')
import os
import agentscope
from agentscope.rag import KnowledgeBank
from agentscope.agents import SciAgent
import numpy as np
import json
from prompt import Prompts
import re
import random
from agentscope.memory import TemporaryMemory
import ollama

from functools import partial
from scientist_utils import (
    extract_scientist_names,
    team_description,
    n2s,
    convert_you_to_other,
    team_description_detail,
    format_msg,
    read_txt_files_as_dict,
    extract_between_json_tags,
    extract_metrics,
    paper_search,
    strip_non_letters,
    save2database,
    count_team
)
from agentscope.message import Msg
from agentscope.msghub import msghub
from agentscope.pipelines.functional import sequentialpipeline

import faiss

from sci_team import Team

class Platform:
    r"""Platform."""

    def __init__(self,
                 model_configuration: str = './configs/model_configs.json',
                 agent_num: int = 1,
                 root_dir: str = '/home/bingxing2/ailab/group/ai4agr/shy/s4s',
                 paper_info_dir: str = 'papers',
                 author_info_dir: str = 'authors',
                 adjacency_matrix_dir: str = 'authors_degree_ge50_from_year2000to2010',
                 agent_model_config_name: str = 'ollama_llama3.1_8b',
                 review_model_config_name: str = 'ollama_llama3.1_70b',
                 knowledgeBank_config_dir: str = "./configs/knowledge_config.json",
                 hop_num: int = 2,
                 group_max_discuss_iteration: int = 1,
                 recent_n_team_mem_for_retrieve: int = 1,
                 team_limit: int = 3,
                 check_iter: int = 2,
                 review_num: int = 2,
                 max_teammember: int = 6,
                 cite_number: int = 8,
                 default_mark: int = 4
                 ):
        self.agent_num = agent_num
        self.paper_info_dir = os.path.join(root_dir, paper_info_dir)
        self.author_info_dir = os.path.join(root_dir, author_info_dir)
        self.adjacency_matrix_dir = os.path.join(root_dir, adjacency_matrix_dir)
        self.group_max_discuss_iteration = group_max_discuss_iteration
        self.recent_n_team_mem_for_retrieve = recent_n_team_mem_for_retrieve
        # how many teams for one agent is allowed
        self.team_limit = team_limit
        # how many times to try paper search
        self.check_iter = check_iter
        # the number of reviewer
        self.reviewer_num = review_num
        # the max team member in a team
        self.max_teammember = max_teammember
        # cite how many paper when generating the idea
        self.cite_number = cite_number
        # default review mark
        self.default_mark = default_mark

        # author2paper file: dict{'authorID':[paperID1, paperID2, ...]}
        with open('{}/author2paper.json'.format(root_dir), 'r') as file:
            self.author2paper = json.load(file)

        # load k-hop adjacency matrix
        self.degree_int2word = ['one', 'two', 'three', 'four', 'five']
        # self.adjacency_matrix = np.loadtxt(
        #     '{}/{}-hop_adj_matrix.txt'.format(self.adjacency_matrix_dir, self.degree_int2word[hop_num-1]), dtype=int)
        self.adjacency_matrix = np.loadtxt(
            '{}/adj_matrix.txt'.format(self.adjacency_matrix_dir), dtype=int)

        # check if agent_num is valid
        if self.agent_num is None:
            self.agent_num = len(self.adjacency_matrix)
        else:
            assert self.agent_num <= len(self.adjacency_matrix)

        # load agentID2authorID file: dict{'agentID': 'authorID'}
        with open('{}/agentID2authorID.json'.format(self.adjacency_matrix_dir), 'r') as file:
            self.agentID2authorID = json.load(file)

        # init agentscope
        agentscope.init(model_configs=model_configuration)

        # init knowledge bank
        if knowledgeBank_config_dir is not None:
            self.knowledge_bank = self.init_knowledgeBank(knowledgeBank_config_dir)

        # init agent pool
        self.agent_pool = [self.init_agent(str(agent_id), agent_model_config_name, self.adjacency_matrix, '/home/bingxing2/ailab/group/ai4agr/crq/SciSci/books/author_{}.txt'.format(agent_id)) for agent_id in range(len(self.adjacency_matrix))]
        self.reviewer_pool = [self.init_reviewer(str(agent_id), review_model_config_name) for agent_id in range(self.reviewer_num)]
        self.id2agent = {}
        for agent in self.agent_pool:
            self.knowledge_bank.equip(agent, agent.knowledge_id_list)
            self.id2agent[agent.name] = agent
        # team pool 
        self.team_pool = []
        agent_id = 1
        for agent in self.agent_pool[:self.agent_num]:
            team_agent = []
            team_index = []
            team_index.append(agent.name)
            team_dic = Team(str(agent_id)+','+str(1))
            team_dic.teammate = team_index
            team_agent.append(team_dic)
            self.team_pool.append(team_agent)
            agent_id = agent_id + 1


        # init hint
        self.HostMsg = partial(Msg, name="user", role="user", echo=True)

        # paper embedding list
        cpu_index = faiss.read_index("/home/bingxing2/ailab/group/ai4agr/crq/SciSci/faiss_index.index")  # 加载索引
        res = faiss.StandardGpuResources()  # 为 GPU 资源分配
        self.gpu_index = faiss.index_cpu_to_gpu(res, 0, cpu_index)  # 将索引移到 GPU

        paper_folder_path = "/home/bingxing2/ailab/group/ai4agr/crq/SciSci/papers"  # 替换为实际的文件夹路径
        self.paper_dicts = read_txt_files_as_dict(paper_folder_path)

    def init_reviewer(self, agent_id, agent_model_config_name):
        agent = SciAgent(
            name='Paper Reviewer{}'.format(agent_id),
            model_config_name=agent_model_config_name,
            sys_prompt=Prompts.prompt_review_system,
        )
        return agent

    def init_agent(self, agent_id, agent_model_config_name, adjacency_matrix, information_path):
        # # load author info
        # with open('{}/{}.json'.format(self.author_info_dir, self.agentID2authorID[agent_id]), 'r') as file:
        #     author_info = json.load(file)

        # author_id = author_info['author_id']
        # author_name = author_info['author_name']
        # author_affiliations = str(author_info['author_affiliations'])
        # research_topics = str(author_info['research_topics'])
        # paper_count = author_info['paper_count']
        # citation_number = author_info['citation_number']
        # connection = adjacency_matrix[int(agent_id)]
        # connection = ['Scientist{}'.format(index) for index, value in enumerate(connection) if value != 0]

        # author_affiliations_new = []
        # for string in author_affiliations:
        #     list_index = string.split(',')
        #     if len(list_index)>2:
        #         list_index = list_index[:-2]
        #     string_index = ','.join(list_index)
        #     author_affiliations_new.append(string_index)
        # author_affiliations_new = list(set(author_affiliations_new))[:3]

        # # prompt
        # prompt = 'Your name is Scientist{}, ' \
        #          'you belong to following affiliations {}, ' \
        #          'you have researched on following topics {}, ' \
        #          'you have published {} papers, ' \
        #          'you have {} citations, '\
        #          'you have previously collaborated with these individuals {}.'.format(agent_id, 
        #                                        author_affiliations_new, 
        #                                        research_topics,
        #                                        paper_count,
        #                                        citation_number,
        #                                        connection)
        with open(information_path, 'r') as file:
            prompt = file.read()

        agent = SciAgent(
            name='Scientist{}'.format(agent_id),
            model_config_name=agent_model_config_name,
            sys_prompt=prompt,
            knowledge_id_list = ["author_information"],
            similarity_top_k=2,
            log_retrieval=False,
            recent_n_mem_for_retrieve=2,
        )

        return agent

    def init_knowledgeBank(self, knowledgeBank_config_dir):
        knowledge_bank = KnowledgeBank(configs="configs/knowledge_config.json")

        # alternatively, we can easily input the configs to add data to RAG
        knowledge_bank.add_data_as_knowledge(
            knowledge_id="author_information",
            emb_model_name="ollama_embedding-mxbai-embed-large",
            data_dirs_and_types={
                "/home/bingxing2/ailab/group/ai4agr/crq/SciSci/books": [".txt"],
            },
        )
        return knowledge_bank

    def select_coauthors(self,):
        team_list = self.team_pool
        scientists = self.agent_pool[:self.agent_num]
        # decide whether the scientist wants to find partners
        for agent_index in range(len(scientists)):
            # avoid too many teams
            if count_team(team_list[agent_index])>=self.team_limit:
                continue
            hint = self.HostMsg(content=Prompts.ask_choice.format_map(
                {
                    "Scientist_name": scientists[agent_index].name,
                    "All_team": team_description(team_list[agent_index])
                },
            ),
            )
            # set_parsers(scientists[agent_index], Prompts.scientist_self_discuss_parser)
            x = scientists[agent_index].reply(hint)
            match = re.search(r'action\s*(\d+)', x.content, re.IGNORECASE)

            # when action2, the agent choose to act independently
            if int(match.group(1))==2:
                print("Single Agent Independently!")
                team_list[agent_index][0].state=2
                continue

            team_candidate = []
            # use prompts to select scientists
            hint = self.HostMsg(content=Prompts.to_scientist_select)
            team_candidate = extract_scientist_names(scientists[agent_index].reply(hint, use_memory = False).content)
            team_candidate_temp = []
            if len(team_candidate)<4:
                for scientist in team_candidate:
                    team_candidate_temp.append(scientist)
                    name = int(scientist[9:])
                    for i in range(len(self.adjacency_matrix)):
                        if (self.adjacency_matrix[name,i]+1)*random.random() > 1.1 and i!=agent_index:
                            team_candidate_temp.append(f"Scientist{i}")
            else:
                team_candidate_temp = team_candidate
            # show all team member candidate
            print(team_candidate_temp)
            team_candidate_after = []
            if len(team_candidate_temp) > 3:
                for i in range(len(team_candidate_temp)):
                    if random.random() > 0.6:
                        team_candidate_after.append(team_candidate_temp[i])
            else:
                team_candidate_after = team_candidate_temp
            team_candidate_after = list(set(team_candidate_after))
            random.shuffle(team_candidate_after)
            team_candidate_after = team_candidate_after[:self.max_teammember]
            print(team_candidate_after)
            is_contained = False
            for agent_list in team_list:
                for sublist in agent_list:
                    if set(sublist.teammate) == set(team_candidate_after) and sublist.state != 6:
                        is_contained = True
                        break
                if is_contained == True:
                    break
            if is_contained == True:
                continue
            # ask each scientist to decide whether to join
            agent_candidate = self.id_to_agent(team_candidate_after)
            # create new team
            team_index = []
            team_index.append(scientists[agent_index].name)
            for agent in agent_candidate:
                if agent.name == scientists[agent_index].name:
                    continue
                hint = self.HostMsg(content=Prompts.to_scientist_choice.format_map({
                    "inviter_name": scientists[agent_index].name,
                    "personal information" : convert_you_to_other(scientists[agent_index].sys_prompt)
                }))
                # set_parsers(agent, Prompts.scientist_invite_parser)
                pattern = re.compile(r'action\s*1', re.IGNORECASE)
                # action1 means a scientist accepts the invitance
                if pattern.search(agent.reply(hint, use_memory=False, use_RAG=False).content):
                    team_index.append(agent.name)
            # delete repeated teams
            is_contained = False
            for agent_list in team_list:
                for sublist in agent_list:
                    if set(sublist.teammate) == set(team_index) and sublist.state != 6:
                        is_contained = True
                        break
                if is_contained == True:
                    break
            if is_contained == False:
                team_dic = Team(str(agent_index+1)+','+str(len(self.team_pool[agent_index])+1))
                team_dic.state=2
                team_dic.teammate = team_index
                team_list[agent_index].append(team_dic)
                # connetion between collaborators will be closer
                for member in team_dic.teammate:
                    if int(member[9:])!=agent_index:
                        self.adjacency_matrix[agent_index,int(member[9:])]=self.adjacency_matrix[agent_index,int(member[9:])]+0.2
                        self.adjacency_matrix[int(member[9:]),agent_index]=self.adjacency_matrix[int(member[9:]),agent_index]+0.2
                # summary current teams in memory
                scientists[agent_index].prompt_reply(self.HostMsg(content=team_description_detail(team_list[agent_index], self.agent_pool)))
            else:
                continue
        return team_list

    def group_discuss(self, team_temp, prompt :str = None):
        # prompt is used to start and guide the discussion
        # for each turn, in group_discuss, all dialogue history is stored in dialogue_history but not in agent memory
        # after finishing each discussion turn, agent1 will summarize dialogue_history and add a summarization into team_history
        team = team_temp
        # get team_history
        team_history = team.memory
        # get teammate
        teammate = self.id_to_agent(team.teammate)
        # init dialogue_history
        dialogue_history = TemporaryMemory(None)
        # init exit state
        exit = False
        # output return dialogue history, summarization of the last turn, and memory of the last turn
        output = {}
        said = []
        # start discussing
        for turn in range(self.group_max_discuss_iteration):
            # init turn_memory for each turn
            turn_history = TemporaryMemory(None)
            agent_num = 0
            for agent in teammate:
                if agent.name in said:
                    continue
                else:
                    said.append(agent.name)
                agent_prompt = format_msg(
                    # current team
                    Msg(name="current team members", role="user", content=','.join(team.teammate)),
                    # team history
                    Msg(name="Summarizations of previous team discussions", role="user", content='')
                    if team_history.size()>0 else None,
                    team_history.get_memory(recent_n=self.recent_n_team_mem_for_retrieve),
                    # prompt
                    Msg(name="user", role="user", content=prompt),
                    # dialogue history
                    Msg(name="Summarizations of previous turns in current team discussion", role="user", content='')
                    if dialogue_history.size()>0 else None,
                    dialogue_history.get_memory(recent_n=turn),
                    # turn history
                    Msg(name="Discussions in this turn", role="user", content='')
                    if turn_history.size()>0 else None,
                    turn_history.get_memory(recent_n=agent_num)
                )
                # add reply to turn_history
                reply = agent.prompt_reply(agent_prompt, add_memory = False, use_memory = False)
                involved_scientist = extract_scientist_names(reply.content)
                print(involved_scientist)
                # judge whether someone is called to join the team
                for scientist_index in involved_scientist:
                    if scientist_index not in team.teammate:
                        if "by the way" in reply.content or "By the way" in reply.content:
                            hint = Msg(name=team.teammate[0],role="user",content=reply.content)
                            # invite new team member to comment
                            if self.id2agent[scientist_index].reply(hint, use_memory=False, use_RAG=False).content is not None:
                                said.append(scientist_index)
                                team.teammate.append(scientist_index)
                                teammate.append(self.id2agent[scientist_index])

                turn_history.add(reply)
                agent_num = agent_num + 1
                # discussion is finished
                if 'exit' in reply:
                    exit = True
                    break

            # summarize this turn's discussion
            history = format_msg(
                # team history
                Msg(name="Summarizations of previous team discussions", role="user", content='')
                if team_history.size()>0 else None,
                team_history.get_memory(recent_n=self.recent_n_team_mem_for_retrieve),
                # dialogue history
                Msg(name="Summarizations of previous turns in current team discussion", role="user", content='')
                if dialogue_history.size()>0 else None,
                dialogue_history.get_memory(recent_n=turn),
            )
            turn_summarization = Msg(name="summarizations of turn{}".format(turn+1), role="user",
                                     content=teammate[0].summarize(history = history, content = turn_history.get_memory(recent_n=agent_num)))

            if exit or turn==self.group_max_discuss_iteration-1:
                output['last_turn_summarization'] = turn_summarization
                output['last_turn_history'] = turn_history
                break
            else:
                dialogue_history.add(turn_summarization)

        output['dialogue_history'] = dialogue_history
        team.teammate = self.agent_to_id(teammate)
        return team, output

    def select_topic(self, team):
        # prompt to start discussing select_topic
        team, discuss_result = self.group_discuss(team, Prompts.to_start_topic_discussion)
        print('finish group discuss')
        team_history = team.memory
        dialogue_history = discuss_result['dialogue_history']
        last_turn_history = discuss_result['last_turn_history']
        last_turn_summarization = discuss_result['last_turn_summarization']

        answer_prompt = format_msg(
            # team history
            Msg(name="Summarizations of previous team discussions", role="user", content='')
            if team_history.size()>0 else None,
            team_history.get_memory(recent_n=self.recent_n_team_mem_for_retrieve),
            # dialogue history
            Msg(name="Summarizations of previous turns in current team discussion", role="user", content='')
            if dialogue_history.size()>0 else None,
            dialogue_history.get_memory(recent_n=self.group_max_discuss_iteration),
            # turn history
            Msg(name="Discussions in this turn", role="user", content='')
            if last_turn_history.size()>0 else None,
            last_turn_history.get_memory(recent_n=last_turn_history.size()),
            # answer_prompt
            Msg(name="user", role="user", content=Prompts.to_ask_if_ready_give_topic)
        )
        answer = self.id2agent[team.teammate[0]].prompt_reply(answer_prompt, add_memory = False, use_memory=False)
        answer_pattern = re.compile(r'action\s*1', re.IGNORECASE)

        # update dialogue history
        dialogue_history.add(last_turn_summarization)
        dialogue_history.add(answer)

        # check whether agent is ready to answer
        if answer_pattern.search(answer.content) or team_history.size()>=1:
            team.state = 3
            history_prompt = format_msg(
                # team history
                Msg(name="Summarizations of previous team discussions", role="user", content='')
                if team_history.size()>0 else None,
                team_history.get_memory(recent_n=self.recent_n_team_mem_for_retrieve),
                # dialogue history
                Msg(name="Summarizations of previous turns in current team discussion", role="user", content='')
                if dialogue_history.size()>0 else None,
                dialogue_history.get_memory(recent_n=self.group_max_discuss_iteration),
                # turn history
                Msg(name="Discussions in this turn", role="user", content='')
                if last_turn_history.size()>0 else None,
                last_turn_history.get_memory(recent_n=last_turn_history.size()),
            )
            topic_prompt = format_msg(
                history_prompt,
                answer,
                # topic_prompt
                Msg(name="user", role="user", content=Prompts.to_ask_topic)
            )
            topic = self.id2agent[team.teammate[0]].prompt_reply(topic_prompt, add_memory = False)
            team.topic = topic.content

            # update dialogue history
            dialogue_history.add(topic)

        # update team_history
        history = format_msg(
            # team history
            Msg(name="Summarizations of previous team discussions", role="user", content='')
            if team_history.size()>0 else None,
            team_history.get_memory(recent_n=self.recent_n_team_mem_for_retrieve)
        )
        team_history.add(Msg(name="summarizations of one topic discussion", role="user",
                             content=self.id2agent[team.teammate[0]].summarize(history = history, content = dialogue_history.get_memory(recent_n=self.group_max_discuss_iteration))))
        team.memory = team_history
        return team

    def generate_idea(self, team):
        topic = team.topic
        old_idea = None
        best_idea = None
        # search related paper about the topic
        selected_topics = topic.split("Selected Topics:")[-1].strip()
        query_vector = ollama.embeddings(model="mxbai-embed-large", prompt=selected_topics)
        query_vector = np.array([query_vector['embedding']])
        D, I = self.gpu_index.search(query_vector, self.cite_number)

        paper_use = []
        for id in range(len(I[0])):
            paper_title = self.paper_dicts[I[0][id]]['title']
            paper_abstract = self.paper_dicts[I[0][id]]['abstract']
            paper_index = {}
            paper_index['title'] = paper_title
            paper_index['abstract'] = paper_abstract
            paper_use.append(paper_index)
        paper_reference = ""
        for id in range(len(paper_use)):
            paper_index = paper_use[id]
            paper_reference = paper_reference+"Paper {}:".format(id+1)+"\n"
            paper_reference = paper_reference+"Title: "+paper_index['title']+"\n"
            paper_reference = paper_reference+"Abstract: "+paper_index['abstract']+"}"+"\n"

        teammate = self.id_to_agent(team.teammate)
        idea_judge = True
        for turn in range(self.group_max_discuss_iteration):
            # discuss the idea
            for agent in teammate:
                idea_prompt = Prompts.prompt_existing_idea.format(old_idea)+Prompts.prompt_task+ \
                              Prompts.prompt_topic.format(selected_topics)+Prompts.prompt_reference.format(paper_reference)+ \
                              Prompts.prompt_response
                agent_prompt = format_msg(
                    # prompt
                    Msg(name="user", role="user", content=idea_prompt),
                )
                reply = agent.prompt_reply(agent_prompt, add_memory = False, use_memory = False, use_RAG=False)
                old_idea = extract_between_json_tags(reply.content, num=1)
                # find the metric
                split_keywords = ['Interestingness', 'Feasibility', 'Novelty']
                metrics = extract_metrics(old_idea, split_keywords)
                if best_idea != None:
                    best_metrics = extract_metrics(best_idea, split_keywords)
                    old_count = 0
                    best_count = 0
                    for split_keywork in split_keywords:
                        old_count = old_count + metrics[split_keyword]
                        best_count = best_count + best_metrics[split_keyword]
                    if old_count>best_count:
                        best_idea = old_idea
                else:
                    best_idea = old_idea
                for split_keyword in split_keywords:
                    if metrics[split_keyword]<8:
                        idea_judge=False
                        break
                if idea_judge:
                    best_idea=old_idea
                    break
            if idea_judge:
                break
        if team.idea == None:
            team.idea = best_idea
        print("Final Idea:")
        print(team.idea)
        team.state=4
        team.citation_id = I[0]
        return team

    def generate_abstract(self, team):
        idea = team.idea
        old_abstract = team.abstract
        teammate = self.id_to_agent(team.teammate)

        for turn in range(self.group_max_discuss_iteration):
            # discuss the abstract
            for agent in teammate:
                if old_abstract == None:
                    abstract_prompt = Prompts.prompt_abstract+"\n"+idea+ \
                                      "\n"+Prompts.prompt_abstract_requirement+"\n"+Prompts.prompt_abstract_response
                else:
                    # the paper is not reviewed by reviewer
                    if team.paper_review == None:
                        # the paper is not reviewer by the team member
                        if team.self_review == None:
                            prompt_abstract_judgement = Prompts.prompt_abstract_judgement.replace("[Insert abstract here]",old_abstract)
                            abstract_prompt = prompt_abstract_judgement+Prompts.prompt_abstract_revise_response
                        else:
                            prompt_abstract_judgement = Prompts.prompt_abstract_judgement_self.replace("[Insert abstract here]",old_abstract)
                            prompt_abstract_judgement = prompt_abstract_judgement.replace("[Insert self_review comments]", team.self_review)
                            abstract_prompt = prompt_abstract_judgement+Prompts.prompt_abstract_revise_response
                    else:
                        prompt_abstract_judgement = Prompts.prompt_abstract_judgement_after_review.replace("[Insert Reviewer comments]",team.paper_review)
                        prompt_abstract_judgement = prompt_abstract_judgement.replace("[Insert abstract here]",old_abstract)
                        abstract_prompt = prompt_abstract_judgement+Prompts.prompt_abstract_revise_response
                agent_prompt = format_msg(
                    # prompt
                    Msg(name="user", role="user", content=abstract_prompt),
                )
                reply = agent.prompt_reply(agent_prompt, add_memory = False, use_memory = False, use_RAG=False)
                old_abstract = extract_between_json_tags(reply.content, num=1)
                if old_abstract == None:
                    old_abstract = reply.content

        # find similar paper
        title = old_abstract.split("Abstract")[0]
        title = strip_non_letters(title.split("Title")[1])
        related_papers = paper_search(title, top_k=self.cite_number)
        iter = 1
        while len(related_papers)==0:
            related_papers = paper_search(title, top_k=self.cite_number)
            iter += 1
            if iter > self.check_iter:
                break
        # find paper successfully
        if len(related_papers)>0:
            abstract_check_prompt = Prompts.prompt_abstract_check.replace("[Insert your abstract here]", old_abstract)
            cite_abstract = ""
            word = ['A','B','C','D','E','F','G','H','I','J','K','L','M','N','O','P','Q','R','S','T']
            split_keywords = []
            for paper_id in range(len(related_papers)):
                cite_abstract = cite_abstract+str(paper_id+1)+". Abstract {}: ".format(word[paper_id])+"Title: "+related_papers[paper_id]['title']+"\n"+"Abstract: "+related_papers[paper_id]['abstract']+"\n"
                split_keywords.append('Written Abstract vs {}'.format(word[paper_id]))
            abstract_check_prompt = abstract_check_prompt.replace("[Insert ref abstract here]", cite_abstract)
            abstract_check_prompt = abstract_check_prompt+"\n"+Prompts.prompt_response_check
            agent_prompt = format_msg(
                # prompt
                Msg(name="user", role="user", content=abstract_check_prompt),
            )
            reply = teammate[0].prompt_reply(agent_prompt, add_memory = False, use_memory = False, use_RAG=False)
            print("abstract_check:")
            print(split_keywords)
            comparison = extract_between_json_tags(reply.content)
            metric = extract_metrics(comparison, split_keywords=split_keywords)
            abstract_use = True
            for split_keyword in split_keywords:
                if metric[split_keyword]>=70:
                    abstract_use = False
                    team.abstract = old_abstract
                    break
            team.abstract = old_abstract
            print('Final Abstract:')
            print(team.abstract)
            if abstract_use:
                team.state=5
                team.self_review=None
            # if the abstract is too similar one time, go to revise, otherwise back to generate idea
            else:
                if team.self_review!=None:
                    team.state=3
                    team.idea = None
                    team.abstract = None
                    team.citation_id = None
                    team.self_review = None
                    team.paper_review = None
                else:
                    team.self_review = reply.content
        else:
            print('Check Fail!!!!!!')
            if team.abstract == None:
                team.abstract = old_abstract
                print('Final Abstract:')
                print(team.abstract)
                team.state=5
        return team

    def generate_review(self, team):
        # paper reviewer by reviewer
        print('current reviewing paper from {}'.format(team.teammate))
        old_abstract = team.abstract
        prompt = Prompts.prompt_review_require_simple.replace("{paper}", old_abstract)
        mark_sum = 0
        team.paper_review==None
        for _ in range(self.reviewer_num):
            agent_prompt = format_msg(
                # prompt
                Msg(name="user", role="user", content=prompt),
            )
            reply = self.reviewer_pool[_].prompt_reply(agent_prompt, add_memory = False, use_memory = False, use_RAG=False)
            split_keywords = ['Overall']
            metric = extract_metrics(reply.content, split_keywords)
            if team.paper_review == None:
                team.paper_review = self.reviewer_pool[_].name+":\n"+reply.content
            else:
                team.paper_review = team.paper_review+"\n"+self.reviewer_pool[_].name+":\n"+reply.content
            for split_keyword in split_keywords:
                if metric[split_keyword] == None:
                    mark_sum = mark_sum + self.default_mark
                else:
                    mark_sum = mark_sum + metric[split_keyword]
        if mark_sum>(4*self.reviewer_num):
            print('paper accept!!!!!!')
            team.state=6
            title = old_abstract.split("Abstract")[0]
            title = strip_non_letters(title.split("Title")[1])
            abstract = strip_non_letters(old_abstract.split("Abstract")[1])
            file_dict={}
            file_dict['title']=title
            file_dict['abstract']=abstract
            file_dict['id'] = len(self.paper_dicts)
            file_dict['authors'] = team.teammate
            file_dict['cite_papers'] = team.citation_id
            self.paper_dicts.append(file_dict)
            # add embedding into list
            embedding_list = []
            response = ollama.embeddings(model="mxbai-embed-large", prompt=abstract)
            embedding_list.append(response["embedding"])
            response = np.array(embedding_list)
            self.gpu_index.add(response)
        else:
            team.state = 4
        return team

    def add_author(self, team):
        # prompt to start discussing select_topic
        discuss_result = self.group_discuss(team, Prompts.to_start_add_author)
        print('finish group discuss')
        team_history = team.memory
        dialogue_history = discuss_result['dialogue_history']
        last_turn_history = discuss_result['last_turn_history']
        last_turn_summarization = discuss_result['last_turn_summarization']

        answer_prompt = format_msg(
            # team history
            Msg(name="Summarizations of previous team discussions", role="user", content='')
            if team_history.size()>0 else None,
            team_history.get_memory(recent_n=self.recent_n_team_mem_for_retrieve),
            # dialogue history
            Msg(name="Summarizations of previous turns in current team discussion", role="user", content='')
            if dialogue_history.size()>0 else None,
            dialogue_history.get_memory(recent_n=self.group_max_discuss_iteration),
            # turn history
            Msg(name="Discussions in this turn", role="user", content='')
            if last_turn_history.size()>0 else None,
            last_turn_history.get_memory(recent_n=last_turn_history.size()),
            # answer_prompt
            Msg(name="user", role="user", content=Prompts.to_ask_if_ready_add_authors)
        )
        answer = self.id2agent[team.teammate[0]].prompt_reply(answer_prompt, add_memory = False, use_memory=False)
        answer_pattern = re.compile(r'action\s*1', re.IGNORECASE)

        # update dialogue history
        dialogue_history.add(last_turn_summarization)
        dialogue_history.add(answer)

        # check whether agent is ready to answer
        if answer_pattern.search(answer.content):
            print("Successfully!")

            # select scientists randomly
            team_candidate = []
            match = re.search(r'Scientist(\d+)', team.teammate[0])
            agent_index = match.group(1)
            for i in range(len(self.adjacency_matrix)):
                if (self.adjacency_matrix[agent_index,i]+1)*random.random() > 1.0 and i!=agent_index:
                    team_candidate.append(f"Scientist{i}")
            print(team_candidate)
            agent_candidate = self.id_to_agent(team_candidate)
            print(len(agent_candidate))

            # create new team
            for agent in agent_candidate:
                team_temp = team.teammate
                hint = self.HostMsg(content=Prompts.to_scientist_choice_add_author.format_map({
                    "inviter_name": self.id2agent[team.teammate[0]].name,
                    "personal information" : convert_you_to_other(self.id2agent[team.teammate[0]].sys_prompt),
                    "team_memory" : team.memory.get_memory(recent_n=1),
                    "team_list" : team.teammate,
                }))
                pattern = re.compile(r'action\s*1', re.IGNORECASE)
                # action1 is to accept the invitation
                if pattern.search(agent.reply(hint, use_RAG=False, use_memory=False).content):
                    print("Successfully!")
                    team_temp.append(agent.name)

                # delete repeated teams
                is_contained = False
                for sub_list in self.team_pool[agent_index]:
                    is_contained = (set(sub_list.teammate) == set(team_temp))
                    if is_contained == True:
                        break
                if is_contained == False:
                    team.teammate.append(agent.name)
        team.state = 4
        return team

    def id_to_agent(self, team_list):
        agent_list = []
        for agent_id in team_list:
            agent_list.append(self.id2agent[agent_id])
        return agent_list

    def agent_to_id(self, team_list):
        agent_list = []
        for agent_id in team_list:
            agent_list.append(agent_id.name)
        return agent_list

    def action_excution(self, value, epoch):
        action_dict = {
            2: self.select_topic,
            3: self.generate_idea,
            4: self.generate_abstract,
            5: self.generate_review
        }
        log_dict = {
            2: 'begin select topic',
            3: 'begin generate idea',
            4: 'begin generate abstract',
            5: 'begin generate review'
        }
        if value>1 and value<6:
            print(f'Epoch{epoch}-------------------{log_dict[value]}')
        return action_dict.get(value, None)

    def running(self, epochs):
        # init team_pool
        print(f'Epoch{-1}-------------------initialize team')
        self.team_pool = self.select_coauthors()
        for epoch in range(epochs):
            # state 6 is an over
            # 1. generate paper review for state 5
            # 2. generate paper abstract for state 4
            # 3. generate idea for state 3
            # 4. select topics for state 2
            # 5. select coauthors for state 1
            for agent_index in range(len(self.team_pool)):
                for team_index in range(len(self.team_pool[agent_index])):
                    action = self.action_excution(self.team_pool[agent_index][team_index].state, epoch)
                    if action is not None:
                        self.team_pool[agent_index][team_index] = action(self.team_pool[agent_index][team_index])
                        print(f'Epoch{epoch}-------------------current action finished')
            print(f'Epoch{epoch}-------------------begin select authors')
            self.team_pool = self.select_coauthors()
            print(f'Epoch{epoch}-------------------current action finished')
        output_dir = "/home/bingxing2/ailab/scxlab0066/SocialScience/database/database.db"
        save2database(self.paper_dicts, output_dir)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   