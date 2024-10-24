import os
import tempfile

from openai_server.backend_utils import structure_to_messages
from openai_server.agent_utils import get_ret_dict_and_handle_files
from openai_server.agent_prompting import get_full_system_prompt, planning_prompt, planning_final_prompt, \
    get_agent_tools

from openai_server.autogen_utils import H2OConversableAgent


def run_autogen_2agent(query=None,
                       visible_models=None,
                       stream_output=None,
                       max_new_tokens=None,
                       authorization=None,
                       chat_conversation=None,
                       text_context_list=None,
                       system_prompt=None,
                       image_file=None,
                       # autogen/agent specific parameters
                       agent_type=None,
                       agent_accuracy=None,
                       autogen_use_planning_prompt=None,
                       autogen_stop_docker_executor=None,
                       autogen_run_code_in_docker=None,
                       autogen_max_consecutive_auto_reply=None,
                       autogen_max_turns=None,
                       autogen_timeout=None,
                       autogen_cache_seed=None,
                       agent_venv_dir=None,
                       agent_code_writer_system_message=None,
                       agent_system_site_packages=None,
                       autogen_code_restrictions_level=None,
                       autogen_silent_exchange=None,
                       client_metadata=None,
                       agent_verbose=None) -> dict:
    if client_metadata:
        print("BEGIN 2AGENT: client_metadata: %s" % client_metadata, flush=True)
    assert agent_type in ['autogen_2agent', 'auto'], "Invalid agent_type: %s" % agent_type
    # raise openai.BadRequestError("Testing Error Handling")
    # raise ValueError("Testing Error Handling")

    # handle parameters from chatAPI and OpenAI -> h2oGPT transcription versions
    assert visible_models is not None, "No visible_models specified"
    model = visible_models  # transcribe early

    if stream_output is None:
        stream_output = False
    assert max_new_tokens is not None, "No max_new_tokens specified"

    # handle AutoGen specific parameters
    if autogen_stop_docker_executor is None:
        autogen_stop_docker_executor = False
    if autogen_run_code_in_docker is None:
        autogen_run_code_in_docker = False
    if autogen_max_consecutive_auto_reply is None:
        autogen_max_consecutive_auto_reply = 40
    if autogen_max_turns is None:
        autogen_max_turns = 40
    if autogen_timeout is None:
        autogen_timeout = 120
    if agent_system_site_packages is None:
        agent_system_site_packages = True
    if autogen_code_restrictions_level is None:
        autogen_code_restrictions_level = 2
    if autogen_silent_exchange is None:
        autogen_silent_exchange = True
    if agent_verbose is None:
        agent_verbose = False
    if agent_verbose:
        print("AutoGen using model=%s." % model, flush=True)
    if autogen_use_planning_prompt is None:
        planning_models = ['claude-3-opus', 'claude-3-5-sonnet', 'gpt-4o', 'o1-preview', 'o1-mini']
        # any pattern matching
        if any(x in model for x in planning_models):
            # sonnet35 doesn't seem to benefit
            autogen_use_planning_prompt = False
        else:
            autogen_use_planning_prompt = True if os.getenv('H2OGPT_DISABLE_PLANNING_STEP') is None else False

    # Create a temporary directory to store the code files.
    # temp_dir = tempfile.TemporaryDirectory().name
    temp_dir = tempfile.mkdtemp()

    # iostream = IOStream.get_default()
    # iostream.print("\033[32m", end="")

    path_agent_tools, list_dir = get_agent_tools()

    if agent_accuracy is None:
        agent_accuracy = 'standard'
    if agent_accuracy == 'quick':
        agent_tools_usage_hard_limits = {k: 1 for k in list_dir}
        agent_tools_usage_soft_limits = {k: 1 for k in list_dir}
        extra_user_prompt = """Do not verify your response, do not check generated plots or images using the ask_question_about_image tool."""
    elif agent_accuracy == 'basic':
        agent_tools_usage_hard_limits = {k: 3 for k in list_dir}
        agent_tools_usage_soft_limits = {k: 2 for k in list_dir}
        extra_user_prompt = """Perform only basic level of verification and basic quality checks on your response.  Files you make and your response can be basic."""
    elif agent_accuracy == 'standard':
        agent_tools_usage_hard_limits = dict(ask_question_about_image=5)
        agent_tools_usage_soft_limits = {k: 5 for k in list_dir}
        extra_user_prompt = ""
    elif agent_accuracy == 'maximum':
        agent_tools_usage_hard_limits = dict(ask_question_about_image=10)
        agent_tools_usage_soft_limits = {}
        extra_user_prompt = ""
    else:
        raise ValueError("Invalid agent_accuracy: %s" % agent_accuracy)

    query = extra_user_prompt + query

    from openai_server.autogen_agents import get_code_execution_agent
    from openai_server.autogen_utils import get_code_executor
    executor = get_code_executor(
        autogen_run_code_in_docker,
        autogen_timeout,
        agent_system_site_packages,
        autogen_code_restrictions_level,
        agent_venv_dir,
        temp_dir,
        agent_tools_usage_hard_limits=agent_tools_usage_hard_limits,
        agent_tools_usage_soft_limits=agent_tools_usage_soft_limits,
    )
    code_executor_agent = get_code_execution_agent(executor, autogen_max_consecutive_auto_reply)

    # FIXME:
    # Auto-pip install
    # Auto-return file list in each turn

    base_url = os.environ['H2OGPT_OPENAI_BASE_URL']  # must exist
    api_key = os.environ['H2OGPT_OPENAI_API_KEY']  # must exist
    if agent_verbose:
        print("base_url: %s" % base_url)
        print("max_tokens: %s" % max_new_tokens)

    system_message, internal_file_names, system_message_parts = \
        get_full_system_prompt(agent_code_writer_system_message,
                               agent_system_site_packages, system_prompt,
                               base_url,
                               api_key, model, text_context_list, image_file,
                               temp_dir, query, autogen_timeout)

    enable_caching = True

    def code_writer_terminate_func(msg):
        # In case code_writer_agent just passed a chatty answer without <FINISHED_ALL_TASKS> mentioned,
        # then code_executor will return empty string as response (since there was no code block to execute).
        # So at this point, we need to terminate the chat otherwise code_writer_agent will keep on chatting.
        return isinstance(msg, dict) and msg.get('content', '') == ''

    code_writer_kwargs = dict(system_message=system_message,
                              llm_config={'timeout': autogen_timeout,
                                          'extra_body': dict(enable_caching=enable_caching,
                                                             client_metadata=client_metadata,
                                                             ),
                                          "config_list": [{"model": model,
                                                           "api_key": api_key,
                                                           "base_url": base_url,
                                                           "stream": stream_output,
                                                           'max_tokens': max_new_tokens,
                                                           'cache_seed': autogen_cache_seed,
                                                           }]
                                          },
                              code_execution_config=False,  # Turn off code execution for this agent.
                              human_input_mode="NEVER",
                              is_termination_msg=code_writer_terminate_func,
                              max_consecutive_auto_reply=autogen_max_consecutive_auto_reply,
                              )

    code_writer_agent = H2OConversableAgent("code_writer_agent", **code_writer_kwargs)

    planning_messages = []
    chat_result_planning = None
    if autogen_use_planning_prompt:
        # setup planning agents
        code_writer_kwargs_planning = code_writer_kwargs.copy()
        # terminate immediately
        update_dict = dict(max_consecutive_auto_reply=1)
        # is_termination_msg=lambda x: True
        code_writer_kwargs_planning.update(update_dict)
        code_writer_agent = H2OConversableAgent("code_writer_agent", **code_writer_kwargs_planning)

        chat_kwargs = dict(recipient=code_writer_agent,
                           max_turns=1,
                           message=planning_prompt(query),
                           cache=None,
                           silent=autogen_silent_exchange,
                           clear_history=False,
                           )
        chat_result_planning = code_executor_agent.initiate_chat(**chat_kwargs)

        # get fresh agents
        code_writer_agent = H2OConversableAgent("code_writer_agent", **code_writer_kwargs)
        code_executor_agent = get_code_execution_agent(executor, autogen_max_consecutive_auto_reply)
        if hasattr(chat_result_planning, 'chat_history') and chat_result_planning.chat_history:
            planning_messages = chat_result_planning.chat_history
            for message in planning_messages:
                if 'content' in message:
                    message['content'] = message['content'].replace('<FINISHED_ALL_TASKS>', '').replace('ENDOFTURN', '')
                if 'role' in message and message['role'] == 'assistant':
                    # replace prompt
                    message['content'] = planning_final_prompt(query)

    # apply chat history
    if chat_conversation or planning_messages:
        chat_messages = structure_to_messages(None, None, chat_conversation, None)
        chat_messages.extend(planning_messages)
        for message in chat_messages:
            if message['role'] == 'user':
                code_writer_agent.send(message['content'], code_executor_agent, request_reply=False, silent=True)
            if message['role'] == 'assistant':
                code_executor_agent.send(message['content'], code_writer_agent, request_reply=False, silent=True)

    chat_kwargs = dict(recipient=code_writer_agent,
                       max_turns=autogen_max_turns,
                       message=query,
                       cache=None,
                       silent=autogen_silent_exchange,
                       clear_history=False,
                       )
    if autogen_cache_seed:
        from autogen import Cache
        # Use DiskCache as cache
        cache_root_path = "./autogen_cache"
        if not os.path.exists(cache_root_path):
            os.makedirs(cache_root_path, exist_ok=True)
        with Cache.disk(cache_seed=autogen_cache_seed, cache_path_root=cache_root_path) as cache:
            chat_kwargs.update(dict(cache=cache))
            chat_result = code_executor_agent.initiate_chat(**chat_kwargs)
    else:
        chat_result = code_executor_agent.initiate_chat(**chat_kwargs)

    if client_metadata:
        print("END 2AGENT: client_metadata: %s" % client_metadata, flush=True)
    ret_dict = get_ret_dict_and_handle_files(chat_result,
                                             chat_result_planning,
                                             model,
                                             temp_dir, agent_verbose, internal_file_names, authorization,
                                             autogen_run_code_in_docker, autogen_stop_docker_executor, executor,
                                             agent_venv_dir, agent_code_writer_system_message,
                                             agent_system_site_packages,
                                             system_message_parts,
                                             autogen_code_restrictions_level, autogen_silent_exchange,
                                             client_metadata=client_metadata)
    if client_metadata:
        print("END FILES FOR 2AGENT: client_metadata: %s" % client_metadata, flush=True)

    return ret_dict
