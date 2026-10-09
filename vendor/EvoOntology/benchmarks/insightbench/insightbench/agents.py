import json
import os
import tempfile
import copy
from pathlib import Path

import pandas as pd

from insightbench.utils import agent_utils as au
from insightbench import prompts
from langchain_core.messages import HumanMessage, SystemMessage
from insightbench.utils.metrics_utils import score_insight
from insightbench import metrics
from insightbench.tceo import InsightSemanticLayer
from insightbench.tceo.tool_chat import ToolCallingChat
from PIL import Image


class Agent:

    def __init__(
        self,
        table=None,
        table_user=None,
        dataset_csv_path=None,
        user_dataset_csv_path=None,
        model_name="gpt-4o",
        goal="I want to find interesting trends in this dataset",
        max_questions=3,
        branch_depth=4,
        n_retries=5,
        savedir=None,
        temperature=0,
        context="This is a dataset that could potentially consist of interesting insights",
        semantic_enabled=False,
        semantic_store_path=None,
        semantic_version=None,
        semantic_max_tool_rounds=12,
        semantic_domain=None,
    ):
        self.goal = goal
        self.max_questions = max_questions

        self.temperature = temperature
        self.model_name = model_name
        self.n_retries = n_retries
        self.branch_depth = branch_depth

        if savedir is None:
            savedir = tempfile.mkdtemp()
        self.savedir = savedir

        self.agent_poirot = AgentPoirot(
            model_name=model_name,
            savedir=savedir,
            goal=goal,
            verbose=os.environ.get("INSIGHTBENCH_VERBOSE", "1") == "1",
            temperature=temperature,
            n_retries=n_retries,
            context=context,
            semantic_enabled=semantic_enabled,
            semantic_store_path=semantic_store_path,
            semantic_version=semantic_version,
            semantic_max_tool_rounds=semantic_max_tool_rounds,
            semantic_domain=semantic_domain,
        )
        if dataset_csv_path is not None or table is not None:
            self.agent_poirot.set_table(
                table=table,
                table_user=table_user,
                dataset_csv_path=dataset_csv_path,
                user_dataset_csv_path=user_dataset_csv_path,
            )

    def get_insights(
        self,
        dataset_csv_path=None,
        user_dataset_csv_path=None,
        table=None,
        table_user=None,
        return_summary=True,
    ) -> tuple:
        """
        run the agent to generate a sequence of questions and answers
        """
        self.agent_poirot.set_table(
            table=table,
            table_user=table_user,
            dataset_csv_path=dataset_csv_path,
            user_dataset_csv_path=user_dataset_csv_path,
        )

        # Prompt 2: Get Root Questions
        root_questions = self.agent_poirot.recommend_questions(
            prompt_method="basic", n_questions=self.max_questions
        )

        # Go through the root questions and generate insights
        for q in root_questions:
            question = q
            for i in range(self.branch_depth):
                if self.agent_poirot.table_user is None:
                    prompt_code_method = "single"
                else:
                    prompt_code_method = "multi"
                _, insight_dict = self.agent_poirot.answer_question(
                    question,
                    prompt_code_method=prompt_code_method,
                    prompt_interpret_method="basic",
                )

                next_questions = self.agent_poirot.recommend_questions(
                    n_questions=self.max_questions,
                    insights_history=[insight_dict],
                    # prompt_method="follow_up_with_type",
                    # question_type="descriptive",
                )
                question = next_questions[
                    self.agent_poirot.select_a_question(next_questions)
                ]

        self.agent_poirot.save_state_dict(
            os.path.join(self.savedir, "insights_history.json")
        )
        pred_insights = [o["insight"] for o in self.agent_poirot.insights_history]
        if return_summary:
            pred_summary = self.summarize(self.agent_poirot.insights_history)
            return pred_insights, pred_summary
        return self.agent_poirot.insights_history

    def get_semantic_trace(self):
        return self.agent_poirot.get_semantic_trace()

    def load_checkpoint(self, savedir):
        self.agent_poirot.load_state_dict(
            os.path.join(savedir, "insights_history.json")
        )

    def summarize(self, pred_insights, method="list", prompt_summarize_method="basic"):
        return self.agent_poirot.summarize(
            pred_insights, method, prompt_summarize_method
        )

    def evaluate_agent_on_summary(
        self, gt_insights_dict, score_name, return_summary=False
    ):
        # Get Summary Evaluation
        pred_summary = self.agent_poirot.summarize()
        gt_summary = gt_insights_dict["flag"]
        score_summary = score_insight(pred_summary, gt_summary, score_name=score_name)

        if return_summary:
            summary_dict = {
                "score_summary": score_summary,
                "pred_summary": pred_summary,
                "gt_summary": gt_summary,
            }
            return score_summary, summary_dict
        return score_summary

    def evaluate_agent_on_notebook(self, gt_flags_dict, score_method="rouge1"):
        """
        Evaluate the agent's performance
        """
        # get groundtruth
        gt_insights = []
        for o in gt_flags_dict["insights"]:
            gt_insights += [o["insight_dict"]["insight"]]

        pred_insights = [o["answer"] for o in self.agent_poirot.insights_history]
        # compute score using score_method
        if score_method == "rouge1":
            return metrics.compute_rouge(pred_insights, gt_insights)
        elif score_method == "g_eval":
            return metrics.compute_g_eval_o2m(pred_insights, gt_insights)
        elif score_method == "llama3_eval":
            return metrics.compute_llama3_eval_o2m(pred_insights, gt_insights)


class AgentPoirot:
    _SEMANTIC_TOOL_STAGES = frozenset({"code_generation"})

    def __init__(
        self,
        savedir=None,
        context="This is a dataset that could potentially consist of interesting insights",
        model_name="gpt-3.5-turbo-0613",
        goal="I want to find interesting trends in this dataset",
        verbose=False,
        temperature=0,
        n_retries=5,
        semantic_enabled=False,
        semantic_store_path=None,
        semantic_version=None,
        semantic_max_tool_rounds=12,
        semantic_domain=None,
    ):
        self.goal = goal
        if savedir is None:
            savedir = tempfile.mkdtemp()
        self.savedir = savedir
        self.context = context

        self.model_name = model_name
        self.temperature = temperature

        self.insights_history = []
        self.verbose = verbose
        self.n_retries = n_retries
        self.semantic_enabled = semantic_enabled
        self.semantic_store_path = semantic_store_path
        self.semantic_version = semantic_version
        self.semantic_max_tool_rounds = semantic_max_tool_rounds
        self.semantic_domain = semantic_domain
        self.semantic_events = []
        self.semantic_layer = None
        self.semantic_manifest = None

    def set_table(
        self,
        table=None,
        table_user=None,
        dataset_csv_path=None,
        user_dataset_csv_path=None,
    ):
        self.table = table
        self.table_user = table_user
        self.dataset_csv_path = dataset_csv_path
        self.user_dataset_csv_path = user_dataset_csv_path

        if table is not None:
            self.table = table
        if table_user is not None:
            self.table_user = table_user
        if dataset_csv_path is not None:
            self.dataset_csv_path = os.path.abspath(dataset_csv_path)
            self.table = pd.read_csv(self.dataset_csv_path)
        if user_dataset_csv_path is not None:
            self.user_dataset_csv_path = os.path.abspath(user_dataset_csv_path)
            self.table_user = pd.read_csv(self.user_dataset_csv_path)

        # schema
        self.schema = au.get_schema(self.table)
        if self.table_user is not None:
            self.user_schema = au.get_schema(self.table_user)
        else:
            self.user_schema = None

        self.semantic_events = []
        if self.semantic_enabled:
            self.semantic_layer = InsightSemanticLayer.from_tables(
                table=self.table,
                table_user=self.table_user,
                store_path=self.semantic_store_path,
                version=self.semantic_version,
                trace_events=self.semantic_events,
                domain=self.semantic_domain,
            )
            self.semantic_manifest = self.semantic_layer.manifest()
            self.schema = au.enrich_schema_with_semantics(
                self.schema, self.semantic_layer._task_bindings
            )
            if self.user_schema is not None:
                self.user_schema = au.enrich_schema_with_semantics(
                    self.user_schema, self.semantic_layer._task_bindings
                )
        else:
            self.semantic_layer = None
            self.semantic_manifest = None

    def _semantic_llm(self, stage):
        if (
            self.semantic_layer is None
            or stage not in self._SEMANTIC_TOOL_STAGES
        ):
            return None
        return ToolCallingChat(
            layer=self.semantic_layer,
            model_name=self.model_name,
            stage=stage,
            temperature=self.temperature,
            max_tool_rounds=self.semantic_max_tool_rounds,
        )

    def get_semantic_trace(self):
        if self.semantic_layer is None:
            return None
        return self.semantic_layer.trace()

    def summarize(self, pred_insights, method="list", prompt_summarize_method="basic"):
        if method == "list":
            chat = au.get_chat_model(self.model_name, self.temperature)

            # Function to format the data
            def format_data(data):
                result = ""
                for i, item in enumerate(data):
                    question_tag = f"<question_{i}>{item['question']}</question_{i}>\n"
                    answer_tag = f"<answer_{i}>{item['answer']}</answer_{i}>\n\n"
                    result += f"{question_tag} {answer_tag}\n"
                return result

            # Format the data and print
            formatted_history = format_data(pred_insights)

            # summary = agent.summarize_insights(method="list")
            content_prompt, system_prompt = prompts.get_summarize_prompt(
                method=prompt_summarize_method
            )
            messages = [
                SystemMessage(content=system_prompt),
                HumanMessage(
                    content=content_prompt.format(
                        context=self.context,
                        goal=self.goal,
                        history=formatted_history,
                    )
                ),
            ]

            def _validate_tasks(out):
                isights = au.extract_html_tags(out, ["insight"])

                # Check that there are insights generated
                if "insight" not in isights:
                    return (
                        out,
                        False,
                        f"Error: you did not generate insights within the <insight></insight> tags.",
                    )
                isights = isights["insight"]
                return (isights, out), True, ""

            insight_list, message = au.chat_and_retry(
                chat, messages, n_retry=3, parser=_validate_tasks
            )

            insights = "\n".join(insight_list)

        return insights

    def select_a_question(self, questions):
        """
        Select a question from the list of questions
        """

        return au.select_a_question(
            questions,
            self.context,
            self.goal,
            [o["question"] for o in self.insights_history],
            self.model_name,
            prompts.SELECT_A_QUESTION_TEMPLATE,
            prompts.SELECT_A_QUESTION_SYSTEM_MESSAGE,
            llm=None,
            semantic_manifest=None,
        )

    def recommend_questions(
        self,
        n_questions=3,
        insights_history=None,
        prompt_method=None,
        question_type=None,
    ):
        """
        Suggest Next Best Questions
        """
        if self.verbose:
            print(f"Generating {n_questions} Questions using {self.model_name}...")

        if insights_history is None:


            questions = au.get_questions(
                prompt_method=prompt_method,
                context=self.context,
                goal=self.goal,
                messages=[],
                schema=self.schema,
                max_questions=n_questions,
                model_name=self.model_name,
                temperature=self.temperature,
                llm=None,
                semantic_manifest=None,
            )
        else:
            # Generate Follow Up Questions
            last_insight = insights_history[-1]

            questions = au.get_follow_up_questions(
                context=self.context,
                goal=self.goal,
                question=last_insight["question"],
                answer=last_insight["answer"],
                schema=self.schema,
                max_questions=n_questions,
                model_name=self.model_name,
                prompt_method=prompt_method,
                question_type=question_type,
                temperature=self.temperature,
                llm=None,
                semantic_manifest=None,
            )
            if self.verbose:
                print(
                    "\nFollowing up on the last insight:\n---------------------------------"
                )
                print(f"Question: {last_insight['question']}\n")
                print(f"Answer: {last_insight['answer']}\n")

        if self.verbose:
            print("\nNext Best Questions:\n-------------------")
            for idx, question in enumerate(questions):
                print(f"{idx+1}. {question}")
            print()

        return questions

    def answer_question(
        self,
        question,
        n_retries=5,
        return_insight_dict=True,
        prompt_code_method="single",
        prompt_interpret_method="interpret",
    ):
        n_retries = self.n_retries
        if self.verbose:
            print(f"Generating Code...")
        # Prompt 3: Generate Code
        code_output_folder = os.path.join(
            self.savedir, f"question_{str(len(self.insights_history))}"
        )


        # baseline:   semantic_layer=None → ToolCallingChat(tools=[execute_python])
        # semantic:   semantic_layer=layer → ToolCallingChat(tools=[browse, resolve, execute_python])
        if self.semantic_layer is not None:
            self.semantic_layer.reset_session()
        try:
            solution = au.generate_code_v2(
                schema=self.schema,
                user_schema=self.user_schema,
                goal=self.goal,
                question=question,
                database_path=os.path.abspath(self.dataset_csv_path),
                user_database_path=(
                    os.path.abspath(self.user_dataset_csv_path)
                    if self.user_dataset_csv_path is not None
                    else None
                ),
                output_folder=code_output_folder,
                model_name=self.model_name,
                prompt_method=prompt_code_method,
                temperature=self.temperature,
                semantic_layer=self.semantic_layer,
                max_tool_rounds=self.semantic_max_tool_rounds,
            )
        except RuntimeError as e:
            if self.verbose:
                print(
                    f"Tool-calling code generation failed: {e}\n"
                    f"Falling back to baseline code generation..."
                )
            solution = au.generate_code(
                schema=self.schema,
                user_schema=self.user_schema,
                goal=self.goal,
                question=question,
                database_path=os.path.abspath(self.dataset_csv_path),
                user_database_path=(
                    os.path.abspath(self.user_dataset_csv_path)
                    if self.user_dataset_csv_path is not None
                    else None
                ),
                output_folder=code_output_folder,
                model_name=self.model_name,
                n_retries=n_retries,
                prompt_method=prompt_code_method,
                temperature=self.temperature,
                llm=None,
                semantic_manifest=None,
            )

        required_artifacts = ("stat.json", "x_axis.json", "y_axis.json", "plot.jpg")
        missing_artifacts = [
            name
            for name in required_artifacts
            if not os.path.isfile(os.path.join(code_output_folder, name))
        ]
        if missing_artifacts:
            raise RuntimeError(
                "Code generation completed without required artifacts: "
                + ", ".join(missing_artifacts)
            )

        # Prompt 4: Interpret Solution
        if self.verbose:
            print("Interpreting Solution...")
        interpretation_dict = au.interpret_solution(
            solution=solution,
            model_name=self.model_name,
            schema=self.schema,
            n_retries=n_retries,
            prompt_method=prompt_interpret_method,
            temperature=self.temperature,
        )
        answer = interpretation_dict["interpretation"]["answer"]

        if self.verbose:
            print("\nSolution\n---------")
            print(f"Question: {question}\n")
            print(f"Answer: {answer}\n")
            print(
                f"Justification: {interpretation_dict['interpretation']['justification']}\n"
            )

        insight_dict = {
            "question": question,
            "answer": answer,
            "insight": interpretation_dict["interpretation"]["insight"],
            "justification": interpretation_dict["interpretation"]["justification"],
            "output_folder": code_output_folder,
        }

        # Save into the savedir
        with open(
            os.path.join(code_output_folder, "insight.json"),
            "w",
            encoding="utf-8",
        ) as json_file:
            json.dump(
                insight_dict,
                json_file,
                indent=4,
                sort_keys=True,
                ensure_ascii=False,
            )

        if self.verbose:
            print(f"Results saved at: {code_output_folder}")

        # add to insights
        self.insights_history += [insight_dict]

        insight_dict = copy.deepcopy(insight_dict)
        insight_dict.update(self.get_insight_objects(insight_dict))

        if return_insight_dict:
            return answer, insight_dict

        return answer["answer"]

    def get_insight_objects(self, insight_dict):
        """
        Get Insight Objects
        """
        if os.path.exists(os.path.join(insight_dict["output_folder"], "plot.jpg")):
            # get plot.jpg
            plot = Image.open(os.path.join(insight_dict["output_folder"], "plot.jpg"))
        else:
            plot = None

        if os.path.exists(os.path.join(insight_dict["output_folder"], "x_axis.jpg")):
            # get x_axis.json
            x_axis = json.load(
                open(
                    os.path.join(insight_dict["output_folder"], "x_axis.json"),
                    "r",
                    encoding="utf-8",
                )
            )
        else:
            x_axis = None

        if os.path.exists(os.path.join(insight_dict["output_folder"], "y_axis.json")):
            # get y_axis.json
            y_axis = json.load(
                open(
                    os.path.join(insight_dict["output_folder"], "y_axis.json"),
                    "r",
                    encoding="utf-8",
                )
            )
        else:
            y_axis = None

        if os.path.exists(os.path.join(insight_dict["output_folder"], "stat.json")):
            try:
                # get stat.json
                stat = json.load(
                    open(
                        os.path.join(insight_dict["output_folder"], "stat.json"),
                        "r",
                        encoding="utf-8",
                    )
                )
            except:
                stat = None
        else:
            stat = None

        # get code.py
        if os.path.exists(os.path.join(insight_dict["output_folder"], "code.py")):
            code = Path(
                insight_dict["output_folder"], "code.py"
            ).read_text(encoding="utf-8")
        else:
            code = None

        insight_object = {
            "plot": plot,
            "x_axis": x_axis,
            "y_axis": y_axis,
            "stat": stat,
            "code": code,
        }
        return insight_object

    def save_state_dict(self, fname):
        payload = self.insights_history
        if self.semantic_layer is not None:
            payload = {
                "insights_history": self.insights_history,
                **self.semantic_layer.trace(),
            }
        with open(fname, "w") as f:
            json.dump(payload, f, indent=4)

    def load_state_dict(self, fname):
        with open(fname, "r") as f:
            payload = json.load(f)
        if isinstance(payload, dict):
            self.insights_history = payload.get("insights_history", [])
            self.semantic_events.clear()
            self.semantic_events.extend(payload.get("semantic_events", []))
        else:
            self.insights_history = payload
