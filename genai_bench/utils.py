import os
import re

from genai_bench.logging import init_logger

logger = init_logger(__name__)


def sanitize_string(input_str: str):
    """
    Sanitize a string to be used in filenames by replacing problematic
    characters.
    """
    return (
        input_str.replace("/", "_").replace(",", "_").replace("(", "").replace(")", "")
    )


# Parameter names whose values are credentials. They must never appear in the
# reconstructed command line (written to experiment_metadata.json) or in the
# "Options you provided:" log block. The CLI accepts the same secrets through
# environment variables, so hiding them here does not change behaviour.
SENSITIVE_PARAMS: frozenset[str] = frozenset(
    {
        "api_key",
        "model_api_key",
        "azure_ad_token",
        "aws_secret_access_key",
        "aws_session_token",
        "security_token",
        "storage_aws_secret_access_key",
        "storage_aws_session_token",
        "storage_azure_account_key",
        "storage_azure_connection_string",
        "storage_azure_sas_token",
        "storage_gcp_credentials_path",
        "github_token",
    }
)


def is_single_experiment_folder(folder_name: str) -> bool:
    """
    Checks whether the folder contains only one experiment by inspecting
    whether the folder has files or subfolders.

    Returns True if it appears to be a single experiment folder, False
    otherwise.
    """
    # Check if the folder has any subdirectories (which would indicate
    # multiple experiments)
    subfolders = [
        f
        for f in os.listdir(folder_name)
        if os.path.isdir(os.path.join(folder_name, f))
    ]
    return len(subfolders) == 0


def safe_eval_prompt(prompt_template: str, item: dict) -> str:
    """
    Safely evaluate a prompt template over a dataset row.

    Supports lambda expressions like: lambda x: x["conversations"][0]["content"]
    and simple field access (prompt_template is a key in the item dict).
    """
    # Handle lambda expressions
    template = prompt_template.strip()
    if template.startswith("lambda"):
        try:
            lambda_part, expr = template.split(":", 1)
            var_name = lambda_part.replace("lambda", "").strip()
            expr = expr.strip()
            # Replace x with context
            expr = re.sub(rf"\b{re.escape(var_name)}\b", "context", expr)
            # Safe evaluation by restricting allowed functions
            safe_dict = {"context": item, "str": str, "len": len}
            result = eval(expr, {"__builtins__": {}}, safe_dict)
            return str(result) if result is not None else ""
        except Exception as e:
            logger.warning(
                f"Failed to evaluate prompt template: {prompt_template}, error: {e}"
            )
            return ""

    # Simple field access
    if template in item:
        return str(item[template])

    return prompt_template
