from typing import Any, List, Set

from genai_bench.data.loaders.base import DatasetFormat, DatasetLoader
from genai_bench.logging import init_logger

logger = init_logger(__name__)


class TextDatasetLoader(DatasetLoader):
    """
    This datasetLoader is responsible for loading prompts from a data source.

    TODO: Add support for prompt lambdas similar to ImageDatasetLoader.
    """

    supported_formats: Set[DatasetFormat] = {
        DatasetFormat.TEXT,
        DatasetFormat.CSV,
        DatasetFormat.JSON,
        DatasetFormat.HUGGINGFACE_HUB,
    }
    media_type = "Text"

    def _process_loaded_data(self, data: Any) -> List[str]:
        """Process data loaded from dataset source."""
        # Handle data from dataset sources. A JSON file holding objects (e.g.
        # [{"prompt": "..."}, ...]) also arrives as a list, so only plain
        # strings can be used as prompts directly; anything else has to go
        # through the column extraction below, otherwise the tagger receives a
        # dict and fails on every request.
        if isinstance(data, list) and all(isinstance(item, str) for item in data):
            return data

        # Handle dictionary data (from CSV files) or HuggingFace datasets
        prompt_column = self.dataset_config.prompt_column

        # A JSON file can also hold a list of records (e.g.
        # [{"text": "..."}, ...]). Reading data[prompt_column] from a list
        # raises, so the column has to be picked from every record instead.
        if (
            isinstance(data, list)
            and data
            and all(isinstance(item, dict) for item in data)
        ):
            available_columns = sorted({key for item in data for key in item})
            if prompt_column is None:
                raise ValueError(
                    "Dataset records are objects, so a prompt column must be "
                    "specified. Pass --prompt-column (or set prompt_column in "
                    f"the dataset config). Available columns: {available_columns}"
                )
            try:
                return [str(item[prompt_column]) for item in data]
            except KeyError as e:
                raise ValueError(
                    f"Column '{prompt_column}' not found in the dataset "
                    f"records. Available columns: {available_columns}"
                ) from e

        try:
            column_data = data[prompt_column]
            # Ensure we return a list of strings
            if isinstance(column_data, list):
                return [str(item) for item in column_data]
            else:
                # For HuggingFace datasets, convert to list
                return list(column_data)
        except (ValueError, KeyError, TypeError) as e:
            # Provide helpful error message with available columns
            if isinstance(data, dict):
                available_columns = list(data.keys())
                raise ValueError(
                    f"Column '{prompt_column}' not found in CSV file. "
                    f"Available columns: {available_columns}"
                ) from e
            else:
                raise ValueError(
                    f"Cannot extract prompts from data: {type(data)}, error: {str(e)}"
                ) from e
