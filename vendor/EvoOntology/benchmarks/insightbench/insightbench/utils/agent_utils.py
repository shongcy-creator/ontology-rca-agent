import os
import numpy as np, pandas as pd, re, json, os, shutil, inspect
import nbformat
import contextlib
import io
import os
import re
import subprocess
import threading
import traceback
import sys

from io import StringIO
from pathlib import Path
from insightbench import prompts
from copy import deepcopy
from typing import Dict
from insightbench import tools
from dateutil.parser import parse
from langchain_core.messages import HumanMessage, SystemMessage
from warnings import warn
from functools import partial
from langchain_core.prompts import PromptTemplate
import httpx
from openai import OpenAI
from pathlib import Path

# from langchain_community.chat_models import ChatOpenAI


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
JSON_MAX_TOKENS = 5000
JSON_MAX_CHARS = JSON_MAX_TOKENS * 4  # 4 chars per token (roughly)


def interpret_solution(
    solution: dict,
    n_retries: int,
    model_name: str,
    prompt_method,
    schema,
    temperature: 0,
) -> str:
    """
    Produce insights for a task based on a solution output by a model

    Parameters:
    -----------
    solution: dict
        The output of the code generation function
    answer_template: dict
        A template for the answer that the human should provide. This template should contain a "results" tag
        that contains a list of expected results in the form of dictionaries. Each dictionary should contain
        the following keys: "name", "description", and "value". The model will be asked to fill in the values.
    model: str
        The name of the model to use (default: gpt-4)
    n_retries: int
        The number of times to retry the interpretation if it fails

    Returns:
    --------
    solution_path: str
        The path to the input solution file, which has been updated with the interpretation

    """
    prompt_template = prompts.get_interpret_prompt(method=prompt_method)
    # create prompt
    prompt = PromptTemplate.from_template(prompt_template)

    insight_prompt = _build_insight_prompt(solution)

    # instantiate llm model
    llm = get_chat_model(model_name, temperature)

    # Get human readable answer
    out, _ = retry_on_parsing_error(
        llm,
        prompt.format(
            goal=solution["goal"],
            question=solution["question"],
            message=solution["code_output"],
            insights=insight_prompt,
            schema=schema,
        ),
        parser=_parse_human_readable_insight,
        n_retries=n_retries,
    )
    solution["interpretation"] = out
    return solution


def extract_python_code_blocks(text):
    """
    Extract and merge Python code blocks from a given text string.

    The function identifies code blocks that start with ``` or ```python and end with ```.
    After extracting the code blocks, it removes the start and end delimiters (```, ```python),
    and merges the code blocks together into a single string.

    Parameters
    ----------
    text : str
        The input string from which Python code blocks need to be extracted.

    Returns
    -------
    str
        A string containing the merged Python code blocks stripped of leading and trailing whitespaces.
        Code blocks are separated by a newline character.

    """

    code_blocks = re.findall(r"```(?:python)?(.*?)```", text, re.DOTALL)
    return "\n".join(block.strip() for block in code_blocks)


def _extract_executable_python(text: str) -> str:
    """Extract executable Python from fenced code blocks only.

    DSML markup (including execute_python) is REJECTED — it should have been
    intercepted by ToolCallingChat if it was a semantic tool call, and
    execute_python DSML is no longer accepted as a valid code format.
    Models must use standard ```python code blocks.
    """
    text = str(text or "")

    # Protocol markup always wins over nested code fences. Otherwise a fenced
    # block inside execute_python DSML would bypass the gateway and reach exec().
    if "DSML" in text.upper():
        return ""

    # Only extract from standard fenced code blocks
    fenced_code = extract_python_code_blocks(text)
    if fenced_code:
        return fenced_code

    return text


class PythonREPL:
    """
    Simulates a standalone Python REPL.

    TODO add a way to pass a random seed to the REPL
    """

    _cwd_lock = threading.Lock()

    def __init__(self):
        self.history = []

    def run(self, command: str, workdir: str = None) -> str:
        """Run command with own globals/locals and returns anything printed."""

        if workdir is not None:
            workdir = os.path.abspath(workdir)

        with PythonREPL._cwd_lock:
            if workdir is not None:
                old_cwd = os.getcwd()
                os.chdir(workdir)

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                try:
                    exec(command, locals())
                    valid = True
                    retry_message = ""
                    self.history.append((command, workdir))
                except Exception as e:
                    valid = False
                    retry_message = traceback.format_exc() + "\n" + str(e)
                finally:
                    if workdir is not None:
                        os.chdir(old_cwd)
            output = buffer.getvalue()

        return output, valid, retry_message

    def clone(self):
        """Clone the REPL from history.

        it is not possible to clone the REPL from the globals/locals because they
        may contain references to objects that cannot be pickled e.g. python modules.
        Instead, we clone the REPL by replaying the history.
        """
        new_repl = PythonREPL()
        # deepcopy of history
        new_repl.history = deepcopy(self.history)

        for command, workdir in self.history:
            new_repl.run(command, workdir=workdir)

        return new_repl


def _execute_command(args):
    """Execute a command and return the stdout, stderr and return code

    Parameters
    ----------
    args : list of str or str, directly passed to subprocess.Popen

    Returns
    -------
    stdout : str
        stdout of the command
    stderr : str
        stderr of the command
    returncode : int
        return code of the command
    """
    try:
        process = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False
        )
    except FileNotFoundError as e:
        return "", str(e), 1

    stdout, stderr = process.communicate()

    # decode bytes object to string
    stdout = stdout.decode("utf-8")
    stderr = stderr.decode("utf-8")

    return stdout, stderr, process.returncode


def pip_install(requirements):
    """Install a list of requirements using pip

    Parameters
    ----------
    requirements : list of str
        List of requirements to install, should be in the format of pip install <requirement>

    Returns
    -------
    stdout : str
        stdout of the command
    valid : bool
        True if the installation was successful, False otherwise
    retry_message : str
        Error message if the installation was not successful
    """

    if isinstance(requirements, str):
        requirements = [requirements]
    retry_messages = []
    stdouts = []
    for req in requirements:
        stdout, stderr, code = _execute_command(["pip", "install", req])
        stdouts.append(stdout)
        if stdout.strip().startswith("Usage:"):
            retry_messages.append(
                f"Seems like there is an error on the pip commandline, it just prints usage. stderr:\n{stderr}. stdout:\n{stdout[-1000:]}"
            )
        if code != 0:
            retry_messages.append(
                f"Error code {code} when installing {req}. stderr:\n{stderr}. stdout:\n{stdout[-1000:]}"
            )

    valid = len(retry_messages) == 0
    retry_message = "\n".join(retry_messages)
    return "\n".join(stdouts), valid, retry_message


# =============================================================================
# Code generation
# =============================================================================
def _normalize_generated_code_paths(code: str, data_paths=None) -> str:
    """Replace generated Windows path literals with forward-slash paths."""
    normalized = code
    for path in data_paths or []:
        if not path:
            continue
        posix_path = os.path.abspath(path).replace("\\", "/")
        windows_path = posix_path.replace("/", "\\")
        normalized = normalized.replace(windows_path, posix_path)
        normalized = normalized.replace(windows_path.replace("\\", "\\\\"), posix_path)
    return normalized


def _code_parser(code, output_folder, data_paths=None):
    """
    A parser that is used to parse the code generated by the LLM
    and determine whether it is acceptable or not

    """
    # Clean output folder
    output_folder = Path(output_folder)
    if output_folder.exists():
        shutil.rmtree(output_folder)
    output_folder.mkdir(parents=True)

    raw_code = str(code or "")

    # An unclosed Markdown fence must not be executed as raw Python.
    if "```" in raw_code and not extract_python_code_blocks(raw_code):
        return (
            "",
            False,
            "FORMAT ERROR: The Python code block is incomplete or unclosed. "
            "Return one complete ```python ... ``` block.",
        )

    # Extract standard fenced Python. DSML is rejected by the protocol boundary.
    code_block = _extract_executable_python(code)

    if not code_block.strip():
        if "DSML" in str(code or "").upper():
            return (
                "",
                False,
                "PROTOCOL ERROR: DSML markup reached the Python boundary. "
                "Retry with a complete registered semantic tool call, or return "
                "the final Python program in one ```python ... ``` block. "
                "The execute_python tool is not registered.",
            )
        return "", False, "The model returned no executable Python code."

    code_block = _normalize_generated_code_paths(code_block, data_paths)

    # Run code and report any errors
    output, valid, retry_message = PythonREPL().run(code_block, workdir=output_folder)
    if not valid:
        return "", valid, retry_message

    # Validate output files
    json_files = [f.name for f in output_folder.glob("*.json")]
    plot_files = [f.name for f in output_folder.glob("*.jpg")]

    try:
        # assert that there is x_axis.json, y_axis.json, and stat.json in json_files
        assert "x_axis.json" in json_files
        assert "y_axis.json" in json_files
        assert "stat.json" in json_files
        assert len(json_files) == 3

        # Check that the total length of all json files is not too long
        json_lengths = [
            len((output_folder / f).read_text(encoding="utf-8"))
            for f in json_files
        ]
        total_json_chars = sum(json_lengths)
        if total_json_chars > JSON_MAX_CHARS:
            return (
                "",
                False,
                f"Error: The total length of your json files cannot exceed {JSON_MAX_CHARS} characters. Here is the total length of each json file: {', '.join(f'{f} ({l} characters)' for f, l in zip(json_files, json_lengths))}.",
            )

        assert len(plot_files) == 1 and "plot.jpg" in plot_files

    except:
        return (
            "",
            False,
            f"Error: Your code did not generate the expected output files. Expected x_axis.json, y_axis.json, and stat.json files.",
        )

    # All checks have passed!
    return output, True, ""


def retry_on_parsing_error(
    llm,
    initial_prompt,
    parser,
    n_retries,
    exception_on_max_retries=True,
):
    """
    Try querying a LLM until it returns a valid value with a maximum number of retries.

    Parameters:
    -----------
    llm : callable
        A langchain LLM model.
    initial_prompt : str
        The initial prompt to send to the LLM.
    parser : callable
        A function taking a message and returning a tuple (value, valid, retry_message),
        where retries will be made until valid is True.
    n_retries : int
        The maximum number of retries.
    exception_on_max_retries : bool
        If True, raise an exception if the maximum number of retries is reached.
        Otherwise, returns "".

    Returns:
    --------
    value : str
        The value returned by the LLM.
    completions : list
        The attempts made by the LLM.

    """
    retry_template = prompts.RETRY_TEMPLATE
    prompt = initial_prompt

    completions = []
    for i in range(n_retries + 1):  # Add one since the initial prompt is not a retry
        # Try to get a valid completion
        completions.append(llm(prompt))
        output, valid, retry_message = parser(completions[-1])

        # If parser execution succeeds return the output
        if valid:
            return output, completions

        # If parser execution fails, produce a new prompt that includes the previous output and the error message

        safe_msg = retry_message.encode("ascii", errors="replace").decode("ascii")
        warn(
            f"Retry {i+1}/{n_retries} - Query failed with error: {safe_msg}",
            RuntimeWarning,
        )
        prompt = retry_template.format(
            initial_prompt=initial_prompt,
            prev_output=completions[-1],
            error=safe_msg,
        )

    if exception_on_max_retries:
        raise RuntimeError(
            f"Could not produce a valid completion after {n_retries + 1} "
            f"attempts. Last error: {retry_message}"
        )
    return retry_message, completions


def _extract_top_values(values, k=5, max_str_len=100):
    """
    Extracts the top k values from a pandas series

    Parameters
    ----------
    values : pandas.Series
        Series to extract top values from
    k : int, optional
        Number of top values to extract, by default 5
    max_str_len : int, optional
        Maximum length of string values (will be truncated), by default 100

    """
    top = values.value_counts().iloc[:k].index.values.tolist()
    top = [x if not isinstance(x, str) else x[:max_str_len] for x in top]
    return top


def get_schema(df):
    """
    Extracts schema from a pandas dataframe

    Parameters
    ----------
    df : pandas.DataFrame
        Dataframe to extract schema from

    Returns
    -------
    list of dict
        Schema for each column in the dataframe

    """
    schema = []

    for col in df.columns:
        info = {
            "name": col,
            "type": df[col].dtype,
            "missing_count": df[col].isna().sum(),
            "unique_count": df[col].unique().shape[0],
        }

        # If the column is numeric, extract some stats
        if pd.api.types.is_numeric_dtype(df[col].dtype):
            info["min"] = df[col].min()
            info["max"] = df[col].max()
            info["mean"] = df[col].mean()
            info["std"] = df[col].std()
        # If the column is a date, extract the min and max
        elif _is_date(df[col].iloc[0]):
            info["min"] = df[col].dropna().min()
            info["max"] = df[col].dropna().max()
        # If the column is something else, extract the top values
        else:
            info["top5_unique_values"] = _extract_top_values(df[col])

        schema.append(info)

    return schema


def enrich_schema_with_semantics(schema, task_bindings) -> list:
    """Add semantic role information to each column in the schema.

    Uses deterministic Binder bindings to annotate each column with its
    semantic term label and a one-line analysis hint.

    Parameters
    ----------
    schema : list of dict
        Raw schema from get_schema()
    task_bindings : list of TaskBinding
        Binder output mapping columns to semantic terms

    Returns
    -------
    list of dict
        Enriched schema with 'semantic_role' field on each column
    """
    binding_map = {}
    for tb in (task_bindings or []):
        key = (tb.column.source, tb.column.name.lower().strip())
        binding_map[key] = tb

    role_hints = {
        "Priority Level": "ordinal severity (use for ranking & monotonic tests)",
        "Workflow State": "lifecycle stage (use for filtering by status)",
        "Assignment Group": "responsible team (use for team comparison)",
        "Department": "organizational unit (use for org-level analysis)",
        "Location": "geographic site (use for regional comparison)",
        "Category": "topic classification (use for distribution analysis)",
        "Identifier": "unique label (use for grouping/joining, NOT aggregation)",
        "Timestamp": "time point (use for trends, seasonality, ordering)",
        "Numeric Measure": "aggregatable value (use for sum/avg/distribution)",
        "Duration": "elapsed time (use for time-span analysis)",
        "Count": "record frequency (use for volume comparison)",
        "Entity": "data record (one row = one entity)",
    }

    enriched = []
    for col in schema:
        new_col = dict(col)
        col_name = col["name"].lower().strip()
        # Try main table first, then user table
        tb = binding_map.get(("main", col_name))
        if tb is None:
            tb = binding_map.get(("user", col_name))

        if tb and tb.term_label:
            hint = role_hints.get(tb.term_label, "categorical grouping")
            new_col["semantic_role"] = f"{tb.term_label} — {hint}"
        else:
            new_col["semantic_role"] = "unknown"
        enriched.append(new_col)
    return enriched


def schema_to_str(schema) -> str:
    """Converts the list of dict to a promptable string.

    Parameters
    ----------
    schema : list of dict
        Schema for each column in the dataframe

    Returns
    -------
    str
        String representation of the schema
    """
    schema_str = ""
    for col in schema:
        schema_str += f"Column: {col['name']} ({col['type']})\n"
        for key, val in col.items():
            if key in ["name", "type"]:
                continue
            schema_str += f"  {key}: {val}\n"
    return schema_str


def _is_date(string):
    """
    Checks if a string is a date

    Parameters
    ----------
    string : str
        String to check

    Returns
    -------
    bool
        True if the string is a date, False otherwise

    """
    try:
        parse(str(string))
        return True
    except ValueError:
        return False


def schema_to_str(schema) -> str:
    """Converts the list of dict to a promptable string.

    Parameters
    ----------
    schema : list of dict
        Schema for each column in the dataframe

    Returns
    -------
    str
        String representation of the schema
    """
    schema_str = ""
    for col in schema:
        schema_str += f"Column: {col['name']} ({col['type']})\n"
        for key, val in col.items():
            if key in ["name", "type"]:
                continue
            schema_str += f"  {key}: {val}\n"
    return schema_str


def convert_messages_to_text(messages):
    """
    Convert a list of messages to a string

    Parameters
    ----------
    messages : list
        List of messages to convert

    Returns
    -------
    str
        String representation of the messages

    """
    return "\n".join(
        [
            (
                f"[INST]\n{m.content}\n[/INST]"
                if m.type in ["system", "agent"]
                else f"\n{m.content}\n"
            )
            for m in messages
        ]
    )


def chat_and_retry(chat, messages, n_retry, parser):
    """
    Retry querying the chat models until it returns a valid value with a maximum number of retries.

    Parameters:
    -----------
        chat: callable
            A langchain chat object taking a list of messages and returning the llm's message.
        messages: list
            The list of messages so far.
        n_retry: int
            The maximum number of retries.
        parser: callable
            A function taking a message and returning a tuple (value, valid, retry_message)
            where value is the parsed value, valid is a boolean indicating if the value is valid and retry_message
            is a message to display to the user if the value is not valid.

    Returns:
    --------
        value: object
            The parsed value.

    Raises:
    -------
        ValueError: if the value could not be parsed after n_retry retries.

    """
    for i in range(n_retry):
        text = convert_messages_to_text(messages)
        answer = chat(text)
        try:
            value, valid, retry_message = parser(answer)
        except Exception as e:
            value, valid, retry_message = None, False, traceback.format_exc() + "\n" + str(e)

        if valid:
            return value

        msg = f"Query failed. Retrying {i+1}/{n_retry}.\n[LLM]:\n{answer}\n[User]:\n{retry_message}"

        warn(msg.encode("ascii", errors="replace").decode("ascii"), RuntimeWarning)

        messages.append(HumanMessage(content=answer))
        messages.append(HumanMessage(content=retry_message))

    return {
        "answer": "Error occured",
        "justification": f"Could not parse a valid value after {n_retry} retries.",
    }


def extract_html_tags(text, keys):
    """Extract the content within HTML tags for a list of keys.

    Parameters
    ----------
    text : str
        The input string containing the HTML tags.
    keys : list of str
        The HTML tags to extract the content from.

    Returns
    -------
    dict
        A dictionary mapping each key to a list of subset in `text` that match the key.

    Notes
    -----
    All text and keys will be converted to lowercase before matching.

    """
    content_dict = {}
    keys = set(keys)
    for key in keys:
        pattern = f"<{key}>(.*?)</{key}>"
        matches = re.findall(pattern, text, re.DOTALL)
        # print(matches)
        if matches:
            content_dict[key] = [match.strip() for match in matches]
    return content_dict


def _parse_human_readable_insight(output):
    """
    A parser that makes sure that the human readable insight is produced in the correct format

    """
    try:
        answer = extract_html_tags(output, ["answer"])
        if "answer" not in answer:
            return (
                "",
                False,
                f"Error: you did not generate answers within the <answer></answer> tags",
            )
        answer = answer["answer"][0]
    except ValueError as e:
        return (
            "",
            False,
            f"The following error occured while extracting the value for the <answer> tag: {str(e)}",
        )

    try:
        justification = extract_html_tags(output, ["justification"])
        if "justification" not in justification:
            return (
                "",
                False,
                f"Error: you did not generate answers within the <justification></justification> tags",
            )
        justification = justification["justification"][0]
    except ValueError as e:
        return (
            "",
            False,
            f"The following error occured while extracting the value for the <justification> tag: {str(e)}",
        )
    try:
        insight = extract_html_tags(output, ["insight"])
        if "insight" not in insight:
            return (
                "",
                False,
                f"Error: you did not generate answers within the <insight></insight> tags",
            )
        insight = insight["insight"][0]
    except ValueError as e:
        return (
            "",
            False,
            f"The following error occured while extracting the value for the <insight> tag: {str(e)}",
        )

    return (
        {"answer": answer, "justification": justification, "insight": insight},
        True,
        "",
    )


def _build_insight_prompt(solution) -> str:
    """
    Gather all plots and statistics produced by the model and format then nicely into text

    """
    insight_prompt = ""
    for i, var in enumerate(solution["vars"]):
        insight_prompt += f"<insight id='{i}'>"
        insight_prompt += f"    <stat>"
        insight_prompt += f"        <name>{var['stat'].get('name', 'n/a')}</name>"
        insight_prompt += f"        <description>{var['stat'].get('description', 'n/a')}</description>"
        stat_val = var["stat"].get("value", "n/a")
        stat_val = stat_val[:50] if isinstance(stat_val, list) else stat_val
        insight_prompt += f"        <value>{stat_val}</value>"
        insight_prompt += f"    </stat>"
        insight_prompt += f"    <plot filename='{var['plot']['name']}'>"
        insight_prompt += f"        <xaxis>"
        insight_prompt += f"            <description>{var['x_axis'].get('description', 'n/a')}</description>"
        x_val = var["x_axis"].get("value", "n/a")
        x_val = x_val[:50] if isinstance(x_val, list) else x_val
        insight_prompt += f"            <value>{x_val}</value>"
        insight_prompt += f"        </xaxis>"
        insight_prompt += f"        <yaxis>"
        insight_prompt += f"            <description>{var['y_axis'].get('description', 'n/a')}</description>"
        y_val = var["y_axis"].get("value", "n/a")
        y_val = y_val[:50] if isinstance(y_val, list) else y_val
        insight_prompt += f"            <value>{y_val}</value>"
        insight_prompt += f"        </yaxis>"
        insight_prompt += f"    </plot>"
        insight_prompt += f"</insight>"
    return insight_prompt


def get_insights(
    context,
    goal,
    messages=[],
    schema=None,
    max_questions=3,
    model_name="gpt-4o",
    temperature=0,
):

    chat = get_chat_model(model_name, temperature)

    prompt = prompts.GET_INSIGHTS_TEMPLATE
    messages = [
        SystemMessage(content=prompts.GET_INSIGHTS_SYSTEM_MESSAGE),
        HumanMessage(
            content=prompt.format(
                context=context, goal=goal, schema=schema, max_questions=max_questions
            )
        ),
    ]

    def _validate_tasks(out):
        isights = extract_html_tags(out, ["insight"])

        # Check that there are insights generated
        if "insight" not in isights:
            return (
                out,
                False,
                f"Error: you did not generate insights within the <insight></insight> tags.",
            )
        isights = isights["insight"]
        print("The insights are:", isights)
        print("Length:", len(isights), "   Max:", max_questions)
        return (isights, out), True, ""

    insights, message = chat_and_retry(
        chat, messages, n_retry=3, parser=_validate_tasks
    )

    return insights


def get_questions(
    prompt_method,
    context,
    goal,
    messages=[],
    schema=None,
    max_questions=10,
    model_name="gpt-4o",
    temperature=0,
    llm=None,
    semantic_manifest=None,
):
    if prompt_method is None:
        prompt_method = "basic"

    prompt, system = prompts.get_question_prompt(method=prompt_method)

    chat = llm or get_chat_model(model_name, temperature)

    content = prompt.format(
        context=context, goal=goal, schema=schema, max_questions=max_questions
    )
    if semantic_manifest:
        content += f"\n\n<semantic_manifest>\n{semantic_manifest}\n</semantic_manifest>"

    messages = [
        SystemMessage(content=system),
        HumanMessage(content=content),
    ]

    def _validate_tasks(out):
        questions = extract_html_tags(out, ["question"])
        if "question" not in questions:
            return (
                out,
                False,
                f"Error: you did not generate questions within the <question></question> tags",
            )
        questions = questions["question"]
        # Check that there are at most max_questions questions
        if len(questions) > max_questions:
            return (
                out,
                False,
                f"Error: you can only ask at most {max_questions} questions, but you asked {len(questions)}.",
            )

        return (questions, out), True, ""

    questions, message = chat_and_retry(
        chat, messages, n_retry=3, parser=_validate_tasks
    )

    return questions


def get_dataset_description(
    prompt,
    system,
    context,
    goal,
    messages=[],
    schema=None,
    model_name="gpt-4o",
    temperature=0,
):

    chat = get_chat_model(model_name, temperature)

    messages = [
        SystemMessage(content=system),
        HumanMessage(content=prompt.format(context=context, goal=goal, schema=schema)),
    ]

    def _validate_tasks(out):
        try:
            questions = extract_html_tags(out, ["description"])["description"]
        except Exception as e:
            return (
                out,
                False,
                f"Error: {str(e)}",
            )

        return (questions, out), True, ""

    data_description, message = chat_and_retry(
        chat, messages, n_retry=4, parser=_validate_tasks
    )

    return data_description


def get_follow_up_questions(
    context,
    goal,
    question,
    answer,
    schema=None,
    max_questions=3,
    model_name="gpt-4o",
    prompt_method=None,
    question_type="descriptive",
    temperature=0,
    llm=None,
    semantic_manifest=None,
):
    if prompt_method is None:
        prompt_method = "follow_up"

    prompt, system = prompts.get_question_prompt(method=prompt_method)
    chat = llm or get_chat_model(model_name, temperature)

    if prompt_method == "follow_up_with_type":
        content = prompt.format(
            context=context,
            goal=goal,
            question=question,
            answer=answer,
            schema=schema,
            max_questions=max_questions,
            question_type=question_type,
        )

    else:
        content = prompt.format(
            context=context,
            goal=goal,
            question=question,
            answer=answer,
            schema=schema,
            max_questions=max_questions,
        )

    if semantic_manifest:
        content += f"\n\n<semantic_manifest>\n{semantic_manifest}\n</semantic_manifest>"

    messages = [
        SystemMessage(content=system),
        HumanMessage(content=content),
    ]

    def _validate_tasks(out):
        questions = extract_html_tags(out, ["question"])["question"]
        # print("The questions are:", questions)
        # print("Length:", len(questions), "   Max:", max_questions)

        # Check that there are at most max_questions questions
        if len(questions) > max_questions:
            return (
                out,
                False,
                f"Error: you can only ask at most {max_questions} questions, but you asked {len(questions)}.",
            )

        return (questions, out), True, ""

    questions, message = chat_and_retry(
        chat, messages, n_retry=3, parser=_validate_tasks
    )

    return questions


def select_a_question(
    questions,
    context,
    goal,
    prev_questions,
    model_name="gpt-4o",
    prompt_template=None,
    system_template=None,
    temperature=0,
    llm=None,
    semantic_manifest=None,
):

    chat = llm or get_chat_model(model_name, temperature)

    followup_questions_formatted = "\n".join(
        [f"{i+1}. {q}\n" for i, q in enumerate(questions)]
    )
    if prev_questions:
        prev_questions_formatted = "\n".join(
            [f"{i+1}. {q}\n" for i, q in enumerate(prev_questions)]
        )
    else:
        prev_questions_formatted = None

    prompt = prompt_template
    content = prompt.format(
        context=context,
        goal=goal,
        prev_questions_formatted=prev_questions_formatted,
        followup_questions_formatted=followup_questions_formatted,
    )
    if semantic_manifest:
        content += f"\n\n<semantic_manifest>\n{semantic_manifest}\n</semantic_manifest>"

    messages = [
        SystemMessage(content=system_template),
        HumanMessage(content=content),
    ]

    def _validate_tasks(out):
        question_id = extract_html_tags(out, ["question_id"])["question_id"][0]
        # Check that there are at most max_questions questions
        if int(question_id) >= len(questions):
            return (
                out,
                False,
                f"Error: selected question index should be between 0-{len(questions)-1}.",
            )
        return (int(question_id), out), True, ""

    question_id, message = chat_and_retry(
        chat, messages, n_retry=3, parser=_validate_tasks
    )
    return question_id


def generate_code(
    schema,
    user_schema,
    goal,
    question,
    database_path,
    user_database_path,
    output_folder,
    n_retries,
    prompt_method=None,
    model_name="gpt-4o",
    temperature=0,
    llm=None,
    semantic_manifest=None,
):
    """
    Solve a task using the naive single step approach

    See main function docstring for more details

    """


    database_path = database_path.replace("\\", "/")
    if user_database_path:
        user_database_path = user_database_path.replace("\\", "/")

    prompt_template = prompts.get_code_prompt(method=prompt_method)

    available_functions = [
        func_name
        for func_name, obj in inspect.getmembers(tools)
        if inspect.isfunction(obj)
    ]
    function_docs = []
    for func_name in available_functions:
        function_docs.append(
            f"{func_name}{inspect.signature(getattr(tools, func_name))}:\n{inspect.getdoc(getattr(tools, func_name))}\n"
            + "=" * 20
            + "\n"
        )
    function_docs = "\n".join(function_docs)

    # instantiate llm model
    llm = llm or get_chat_model(model_name, temperature)

    # create prompt
    if user_schema is None:
        prompt = PromptTemplate(
            template=prompt_template,
            input_variables=[
                "goal",
                "schema",
                "question",
                "database_path",
                "function_docs",
            ],
        )
    else:
        prompt = PromptTemplate(
            template=prompt_template,
            input_variables=[
                "goal",
                "schema",
                "question",
                "database_path",
                "function_docs",
                "user_schema",
                "user_database_path",
            ],
        )

    def _with_semantics(content):
        if semantic_manifest:
            return (
                content
                + f"\n\n<semantic_manifest>\n{semantic_manifest}\n</semantic_manifest>"
            )
        return content

    # Run the retry on error function
    if user_schema is None:
        output, completions = retry_on_parsing_error(
            llm=llm,
            initial_prompt=_with_semantics(
                prompt.format(
                    goal=goal,
                    schema=schema,
                    question=question,
                    database_path=database_path,
                    function_docs=function_docs,
                )
            ),
            parser=partial(
                _code_parser,
                output_folder=output_folder,
                data_paths=[database_path, user_database_path],
            ),
            n_retries=n_retries,
            exception_on_max_retries=True,
        )
    else:
        output, completions = retry_on_parsing_error(
            llm=llm,
            initial_prompt=_with_semantics(
                prompt.format(
                    goal=goal,
                    schema=schema,
                    question=question,
                    database_path=database_path,
                    function_docs=function_docs,
                    user_schema=user_schema,
                    user_database_path=user_database_path,
                )
            ),
            parser=partial(
                _code_parser,
                output_folder=output_folder,
                data_paths=[database_path, user_database_path],
            ),
            n_retries=n_retries,
            exception_on_max_retries=True,
        )

    # Create the output dict
    # Then, iterate over all generated plots and add them to the output dict
    code = _extract_executable_python(completions[-1])
    output_dict = {
        "code": code,
        "prompt": str(prompt),
        "code_output": output,
        "message": output,
        "n_retries": len(completions) - 1,
        "goal": goal,
        "question": question,
        "vars": [],
    }

    # Write generated code as UTF-8. Windows' default GBK encoding cannot
    # represent characters such as non-breaking hyphens or superscripts.
    Path(output_folder, "code.py").write_text(code.strip(), encoding="utf-8")

    # Try to load the model's output files
    # TODO: We should detect errors in such files and trigger a retry
    try:
        stat = json.load(open(f"{output_folder}/stat.json", "r", encoding="utf-8"))
    except Exception as e:
        print(f"Failed to load {output_folder}/stat.json", e)
        stat = {}
    try:
        x_axis = json.load(
            open(f"{output_folder}/x_axis.json", "r", encoding="utf-8")
        )
    except Exception as e:
        print(f"Failed to load {output_folder}/x_axis.json", e)
        x_axis = {}
    try:
        y_axis = json.load(
            open(f"{output_folder}/y_axis.json", "r", encoding="utf-8")
        )
    except Exception as e:
        print(f"Failed to load {output_folder}/y_axis.json", e)
        y_axis = {}

    # Add the plot to the final output dict
    plot_path = f"{output_folder}/plot.jpg"
    stat["type"] = "stat"
    x_axis["type"] = "x_axis"
    y_axis["type"] = "y_axis"
    plot_dict = {"name": plot_path, "type": "plot"}
    output_dict["vars"] += [
        {
            "stat": stat,
            "x_axis": x_axis,
            "y_axis": y_axis,
            "plot": plot_dict,
        }
    ]

    return output_dict


# =============================================================================

# =============================================================================

def _build_semantic_guidance(layer) -> str:
    """Build semantic guidance."""
    tools_desc = "\n".join(
        f"- {t['function']['name']}: {t['function']['description']}"
        for t in layer.tool_schemas()
    )
    return f"""## Ontology Layer (version: {layer.version})

Available semantic tools:
{tools_desc}

These tools are provided alongside execute_python through the same
function-calling protocol.

### Suggested Use

1. Call browse_semantics or resolve_semantics to verify which columns
   represent the concepts you need, before calling execute_python.
2. Column bindings from resolve_semantics are deterministic. When it says
   the revenue column is "Amount", use "Amount" exactly — column names
   from the ontology layer are dependable, not guesses.
3. Validate the data by running code. The ontology layer tells you which
   columns to use; the data itself tells you what the values are.
4. One browse + one resolve is usually sufficient. Re-browsing the same
   concept returns the same result — the layer does not change during
   a single code-generation session.
5. If browse/resolve returns no match, proceed with your best guess from
   the schema. The ontology layer does not cover every possible concept.
"""


def generate_code_v2(
    schema,
    user_schema,
    goal,
    question,
    database_path,
    user_database_path,
    output_folder,
    model_name,
    prompt_method=None,
    temperature=0,
    semantic_layer=None,
    max_tool_rounds=12,
):
    """Generate code v2."""
    from insightbench.tceo.tool_chat import ToolCallingChat, EXECUTE_PYTHON_SCHEMA


    database_path = str(database_path).replace("\\", "/")
    if user_database_path:
        user_database_path = str(user_database_path).replace("\\", "/")

    prompt_template = prompts.get_code_prompt(method=prompt_method)

    available_functions = [
        func_name
        for func_name, obj in inspect.getmembers(tools)
        if inspect.isfunction(obj)
    ]
    function_docs = []
    for func_name in available_functions:
        function_docs.append(
            f"{func_name}{inspect.signature(getattr(tools, func_name))}:\n"
            f"{inspect.getdoc(getattr(tools, func_name))}\n"
            + "=" * 20
            + "\n"
        )
    function_docs = "\n".join(function_docs)


    if user_schema is None:
        prompt = PromptTemplate(
            template=prompt_template,
            input_variables=[
                "goal", "schema", "question", "database_path", "function_docs",
            ],
        )
        content = prompt.format(
            goal=goal,
            schema=schema,
            question=question,
            database_path=database_path,
            function_docs=function_docs,
        )
    else:
        prompt = PromptTemplate(
            template=prompt_template,
            input_variables=[
                "goal", "schema", "question", "database_path", "function_docs",
                "user_schema", "user_database_path",
            ],
        )
        content = prompt.format(
            goal=goal,
            schema=schema,
            question=question,
            database_path=database_path,
            function_docs=function_docs,
            user_schema=user_schema,
            user_database_path=user_database_path,
        )


    if semantic_layer is not None:
        content += "\n\n" + _build_semantic_guidance(semantic_layer)
    else:

        content += (
            "\n\nIMPORTANT: Use execute_python to run your analysis code. "
            "The code must save these files to the current directory: "
            "plot.jpg, stat.json, x_axis.json, y_axis.json. "
            "When all artifacts are generated, confirm completion in plain text "
            "without calling any more tools."
        )


    output_folder_path = Path(output_folder)
    if output_folder_path.exists():
        shutil.rmtree(output_folder_path)
    output_folder_path.mkdir(parents=True)



    # semantic: layer=layer → browse + resolve + execute_python
    chat = ToolCallingChat(
        layer=semantic_layer,
        model_name=model_name,
        stage="code_generation",
        temperature=temperature,
        max_tool_rounds=max_tool_rounds,
        execution_tools=[EXECUTE_PYTHON_SCHEMA],
        workdir=str(output_folder_path),
    )


    try:
        final_text = chat(content)
    except RuntimeError:

        raise


    code = chat._last_executed_code or ""
    code_output = chat._last_execution_output or ""


    required_artifacts = ["stat.json", "x_axis.json", "y_axis.json", "plot.jpg"]
    missing = [
        a for a in required_artifacts
        if not output_folder_path.joinpath(a).is_file()
    ]

    if missing:
        if not code:
            raise RuntimeError(
                "ToolCallingChat returned text without calling execute_python. "
                "No Python code was executed — no artifacts were generated."
            )
        else:
            raise RuntimeError(
                f"Code executed but required artifacts missing: {missing}. "
                f"Last execution output: {code_output[:500]}"
            )


    Path(output_folder, "code.py").write_text(code.strip(), encoding="utf-8")


    output_dict = {
        "code": code,
        "prompt": str(prompt),
        "code_output": code_output,
        "message": code_output,
        "n_retries": max(0, chat._execute_python_count - 1),
        "goal": goal,
        "question": question,
        "vars": [],
    }


    try:
        stat = json.load(
            open(f"{output_folder}/stat.json", "r", encoding="utf-8")
        )
    except Exception as e:
        print(f"Failed to load {output_folder}/stat.json", e)
        stat = {}
    try:
        x_axis = json.load(
            open(f"{output_folder}/x_axis.json", "r", encoding="utf-8")
        )
    except Exception as e:
        print(f"Failed to load {output_folder}/x_axis.json", e)
        x_axis = {}
    try:
        y_axis = json.load(
            open(f"{output_folder}/y_axis.json", "r", encoding="utf-8")
        )
    except Exception as e:
        print(f"Failed to load {output_folder}/y_axis.json", e)
        y_axis = {}

    stat["type"] = "stat"
    x_axis["type"] = "x_axis"
    y_axis["type"] = "y_axis"
    plot_path = f"{output_folder}/plot.jpg"
    plot_dict = {"name": plot_path, "type": "plot"}
    output_dict["vars"] += [
        {
            "stat": stat,
            "x_axis": x_axis,
            "y_axis": y_axis,
            "plot": plot_dict,
        }
    ]

    return output_dict


# def root_depth_to_prompt(
#     history: Dict,
#     root: int,
#     depth: int,
#     goal: str,
#     csv_path: str,
#     results_dir: str,
# ) -> Dict:
#     """Get question from a given depth in the history and convert it to the prompt format"""
#     context = "This is a dataset of ServiceNow incidents that contains different types of failure categories"
#     import prompts

#     node = f"{root}{depth}"
#     node_output = history[node]
#     # extract question being asked at this node
#     question = node_output["question"]
#     # extract the data_df
#     data_df = pd.read_csv(csv_path)
#     prompts_dict = {}
#     # now, reconstruct the prompts used in different stages at this node
#     if root == 0 and depth == 0:
#         get_questions_prompt = prompts.GET_QUESTIONS_TEMPLATE
#         get_questions_system = prompts.GET_QUESTIONS_SYSTEM_MESSAGE
#         messages = [
#             SystemMessage(content=get_questions_system),
#             HumanMessage(
#                 content=get_questions_prompt.format(
#                     context=context,
#                     goal=goal,
#                     schema=get_schema(data_df),
#                     max_questions=3,
#                 )
#             ),
#         ]
#         prompts_dict["get_questions"] = messages
#     else:
#         prompts_dict["get_questions"] = None

#     # prompt for generating code
#     code_prompt = prompts.GENERATE_CODE_TEMPLATE
#     available_functions = [
#         func_name
#         for func_name, obj in inspect.getmembers(tools)
#         if inspect.isfunction(obj)
#     ]
#     function_docs = []
#     for func_name in available_functions:
#         function_docs.append(
#             f"{func_name}{inspect.signature(getattr(tools, func_name))}:\n{inspect.getdoc(getattr(tools, func_name))}\n"
#             + "=" * 20
#             + "\n"
#         )
#     function_docs = "\n".join(function_docs)
#     template = prompts.GENERATE_CODE_TEMPLATE
#     code_prompt = PromptTemplate(
#         template=template,
#         input_variables=[
#             "goal",
#             "schema",
#             "question",
#             "database_path",
#             "function_docs",
#         ],
#     )
#     schema = get_schema(data_df)
#     prompts_dict["generate_code"] = code_prompt.format(
#         goal=goal,
#         schema=schema,
#         question=question,
#         database_path=csv_path,
#         function_docs=function_docs,
#     )

#     output_folder = os.path.join(results_dir, node)
#     solution_dict = {
#         "code": open(os.path.join(output_folder, "code.py")).read(),
#         "prompt": str(code_prompt),
#         "code_output": "N/A",
#         "message": "N/A",
#         "n_retries": 3,
#         "goal": goal,
#         "question": question,
#         "vars": [],
#     }

#     # Extract the IDs of all generated plots
#     plot_ids = set(
#         [
#             os.path.splitext(f)[0].split("_")[-1]
#             for f in os.listdir(output_folder)
#             if any(w in f for w in ["plot_", "stat_", "x_axis_", "y_axis_"])
#         ]
#     )

#     for pid in plot_ids:
#         # Try to load the model's output files
#         # TODO: We should detect errors in such files and trigger a retry
#         try:
#             stat = json.load(open(f"{output_folder}/stat_{pid}.json", "r"))
#         except Exception as e:
#             print(f"Failed to load {output_folder}/stat_{pid}.json", e)
#             stat = {}
#         try:
#             x_axis = json.load(open(f"{output_folder}/x_axis_{pid}.json", "r"))
#         except Exception as e:
#             print(f"Failed to load {output_folder}/x_axis_{pid}.json", e)
#             x_axis = {}
#         try:
#             y_axis = json.load(open(f"{output_folder}/y_axis_{pid}.json", "r"))
#         except Exception as e:
#             print(f"Failed to load {output_folder}/y_axis_{pid}.json", e)
#             y_axis = {}

#         # Add the plot to the final output dict
#         plot_path = f"{output_folder}/plot_{pid}.jpg"
#         stat["type"] = "stat"
#         x_axis["type"] = "x_axis"
#         y_axis["type"] = "y_axis"
#         plot_dict = {"name": plot_path, "type": "plot"}
#         solution_dict["vars"] += [
#             {
#                 "stat": stat,
#                 "x_axis": x_axis,
#                 "y_axis": y_axis,
#                 "plot": plot_dict,
#             }
#         ]

#     # prompt for interpreting soln
#     interpret_prompt = prompts.INTERPRET_SOLUTION
#     insight_prompt = _build_insight_prompt(solution_dict)
#     prompts_dict["interpret_solution_prompt"] = interpret_prompt.format(
#         goal=solution_dict["goal"],
#         question=solution_dict["question"],
#         message=solution_dict["message"],
#         insights=insight_prompt,
#     )

#     # build data analysis prompt
#     data_analysis_prompt = prompts.DATA_ANALYTICS_TEMPLATE
#     da_messages = [
#         SystemMessage(content=prompts.GET_DATA_ANALYTICS_SYSTEM_MESSAGE),
#         HumanMessage(
#             content=data_analysis_prompt.format(
#                 context=context,
#                 goal=goal,
#                 schema=schema,
#                 question=question,
#                 answer=node_output["answer"]["answer"],
#                 justification=node_output["answer"]["justification"],
#                 max_questions=3,
#             )
#         ),
#     ]

#     prompts_dict["data_analysis"] = da_messages

#     # get select a follow up prompt
#     select_follow_up_prompt = prompts.SELECT_A_QUESTION_TEMPLATE
#     followup_questions_formatted = "\n".join(
#         [f"{i+1}. {q}\n" for i, q in enumerate(node_output["follow_ups"])]
#     )
#     prev_questions_formatted = "\n".join(
#         [
#             f"{i+1}. {q}\n"
#             for i, q in enumerate(
#                 output["question"]
#                 for node, output in history.items()
#                 if (int(node[0]) <= int(root) and int(node[1]) <= depth)
#             )
#         ]
#     )
#     messages = [
#         SystemMessage(content=prompts.SELECT_A_QUESTION_SYSTEM_MESSAGE),
#         HumanMessage(
#             content=select_follow_up_prompt.format(
#                 context=context,
#                 goal=goal,
#                 prev_questions_formatted=prev_questions_formatted,
#                 followup_questions_formatted=followup_questions_formatted,
#             )
#         ),
#     ]

#     prompts_dict["select_question"] = messages

#     # build the G-Eval prompt
#     curr_answer = node_output["answer"]["answer"]
#     scores_dict = json.load(open(os.path.join(results_dir, "scores.json")))
#     all_gts = [n["gt"] for gt_id, n in scores_dict.items()]

#     geval_prompt = prompts.G_EVAL_TEMPLATE
#     prompts_dict["geval_prompts"] = [
#         prompts.G_EVAL_SYSTEM_MESSAGE,
#         [geval_prompt.format(answer=curr_answer, gt_answer=gt) for gt in all_gts],
#     ]

#     return prompts_dict


def analysis_nb_to_gt(fname_notebook, include_df_head=False) -> None:
    """
    Reads all ipynb files in data_dir and parses each cell and converts it into a ground truth file.
    The ipynb files are structured as follows: code (outputs plot), then a cell with an insight dict
    """

    def _extract_metadata(nb):
        # iterate through the cells
        metadata = {}
        # extract metadata

        # extract name of the dataset from the first cell
        dname = re.findall(r"## (.+) \(Flag \d+\)", nb.cells[0].source)[0].strip()
        metadata["dataset_name"] = dname
        # extract dataset description
        description = (
            re.findall(
                r"(Dataset Overview|Description)(.+)(Your Objective|Task)",
                nb.cells[0].source,
                re.DOTALL,
            )[0][1]
            .replace("#", "")
            .strip()
        )
        metadata["dataset_description"] = description

        # extract goal and role
        metadata["goal"] = re.findall(r"Goal|Objective\**:(.+)", nb.cells[0].source)[
            0
        ].strip()
        metadata["role"] = re.findall(r"Role\**:(.+)", nb.cells[0].source)[0].strip()

        metadata["difficulty"] = re.findall(
            r"Difficulty|Challenge Level\**: (\d) out of \d", nb.cells[0].source
        )[0].strip()
        metadata["difficulty_description"] = (
            re.findall(
                r"Difficulty|Challenge Level\**: \d out of \d(.+)", nb.cells[0].source
            )[0]
            .replace("*", "")
            .strip()
        )
        metadata["dataset_category"] = re.findall(
            r"Category\**: (.+)", nb.cells[0].source
        )[0].strip()

        # Get Dataset Info
        tag = r"^dataset_path =(.+)"

        dataset_csv_path = None
        for cell in nb.cells:
            if cell.cell_type == "code":
                if re.search(tag, cell.source):
                    dataset_csv_path = (
                        re.findall(tag, cell.source)[0]
                        .strip()
                        .replace("'", "")
                        .replace('"', "")
                    )
                    break
        assert dataset_csv_path is not None
        metadata["dataset_csv_path"] = dataset_csv_path

        if include_df_head:
            metadata["df_head"] = pd.read_html(
                StringIO(cell.outputs[0]["data"]["text/html"])
            )

        # Get Dataset Info
        tag = r"user_dataset_path =(.+)"

        user_dataset_csv_path = None
        for cell in nb.cells:
            if cell.cell_type == "code":
                if re.search(tag, cell.source):
                    user_dataset_csv_path = (
                        re.findall(tag, cell.source)[0]
                        .strip()
                        .replace("'", "")
                        .replace('"', "")
                    )
                    break
        metadata["user_dataset_csv_path"] = user_dataset_csv_path

        # Get Summary of Findings
        tag = r"Summary of Findings \(Flag \d+\)(.+)"

        flag = None
        for cell in reversed(nb.cells):
            if cell.cell_type == "markdown":
                if re.search(tag, cell.source, re.DOTALL | re.IGNORECASE):
                    flag = (
                        re.findall(tag, cell.source, re.DOTALL | re.IGNORECASE)[0]
                        .replace("#", "")
                        .replace("*", "")
                        .strip()
                    )
                    break
        assert flag is not None
        metadata["flag"] = flag

        return metadata

    def _parse_question(nb, cell_idx):
        qdict = {}
        qdict["question"] = (
            re.findall(
                r"Question( |-)(\d+).*:(.+)", nb.cells[cell_idx].source, re.IGNORECASE
            )[0][2]
            .replace("*", "")
            .strip()
        )

        if nb.cells[cell_idx + 2].cell_type == "code":
            # action to take to answer the question
            assert nb.cells[cell_idx + 1].cell_type == "markdown"
            qdict["q_action"] = nb.cells[cell_idx + 1].source.replace("#", "").strip()
            assert nb.cells[cell_idx + 2].cell_type == "code"
            qdict["code"] = nb.cells[cell_idx + 2].source
            # extract output plot. Note that this image data is in str,
            # will need to use base64 to load this data

            qdict["plot"] = nb.cells[cell_idx + 2].outputs
            # loop as there might be multiple outputs and some might be stderr
            for o in qdict["plot"]:
                if "data" in o and "image/png" in o["data"]:
                    qdict["plot"] = o["data"]["image/png"]
                    break

            # extract the insight
            try:
                qdict["insight_dict"] = json.loads(nb.cells[cell_idx + 4].source)
            except Exception as e:
                # find the next cell with the insight dict
                for cell in nb.cells[cell_idx + 3 :]:
                    try:
                        qdict["insight_dict"] = json.loads(cell.source)
                        break
                    except Exception as e:
                        continue

        else:
            # print(f"Found prescriptive insight in {fname_notebook}")
            qdict["insight_dict"] = {
                "data_type": "prescriptive",
                "insight": nb.cells[cell_idx + 1].source.strip(),
                "question": qdict["question"],
            }
        return qdict

    def _parse_notebook(nb):
        gt_dict = _extract_metadata(nb)

        # extract questions, code, and outputs
        que_indices = [
            idx
            for idx, cell in enumerate(nb.cells)
            if cell.cell_type == "markdown"
            and re.search(r"Question( |-)\d+", cell.source, re.IGNORECASE)
        ]
        gt_dict["insights"] = []
        for que_idx in que_indices:
            gt_dict["insights"].append(_parse_question(nb, que_idx))
        return gt_dict

    # Convert the notebook to a ground truth file
    if not fname_notebook.endswith(".ipynb"):
        raise ValueError("The file must be an ipynb file")
    else:
        # extract dataset id from flag-analysis-i.ipynb using re
        fname_json = fname_notebook.replace(".ipynb", ".json")

        with open(fname_notebook, "r") as f:
            notebook = nbformat.read(f, as_version=4)
        gt_dict = _parse_notebook(notebook)

    return gt_dict


def _get_agent_api_key():
    """Return agent api key."""
    return os.getenv("AGENT_API_KEY") or os.getenv("OPENAI_API_KEY")


def _get_agent_base_url():
    """Return agent base url."""
    return os.getenv("AGENT_BASE_URL") or os.getenv("OPENAI_BASE_URL")


def _get_verify_ssl():
    """Return verify ssl."""
    val = os.getenv("OPENAI_SSL_VERIFY", "true").lower()
    return val not in ("false", "0", "no", "off")


def get_chat_model(model_name, temperature=0):
    """Return chat model."""
    verify_ssl = _get_verify_ssl()
    http_client = httpx.Client(verify=verify_ssl, trust_env=False)
    kwargs = {
        "api_key": _get_agent_api_key(),
        "http_client": http_client,
        "max_retries": 3,
        "timeout": float(os.getenv("OPENAI_TIMEOUT", "120")),
    }
    base_url = _get_agent_base_url()
    if base_url:
        kwargs["base_url"] = base_url
    client = OpenAI(**kwargs)
    llm = (
        lambda content: client.chat.completions.create(
            model=model_name,
            temperature=temperature,
            messages=[{"role": "user", "content": content}],
        )
        .choices[0]
        .message.content
    )
    return llm


class SuppressOutput:
    def __enter__(self):
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr
        sys.stdout = open(os.devnull, "w")
        sys.stderr = open(os.devnull, "w")

    def __exit__(self, exc_type, exc_value, traceback):
        sys.stdout.close()
        sys.stderr.close()
        sys.stdout = self._original_stdout
        sys.stderr = self._original_stderr


if __name__ == "__main__":
    # dataset_id = 1
    # results_dir = f"./.tmp/outputs_no_goal/gpt-4-turbo-2024-04-09/{dataset_id}/"
    # data_dir = "/mnt/cba/data/servicenow_incidents/flags"
    # goal = json.load(open(os.path.join(data_dir, f"gt_flag_{dataset_id}.json")))["Goal"]
    # history = json.load(open(os.path.join(results_dir, "history.json")))
    # root_depth_to_prompt(
    #     history=history,
    #     root=1,
    #     depth=3,
    #     goal=goal,
    #     csv_path=os.path.join(data_dir, f"flag-{dataset_id}.csv"),
    #     results_dir=results_dir,
    # )

    analysis_nb_to_gt("/mnt/home/projects/research-cba/.tmp/new_notebooks")
