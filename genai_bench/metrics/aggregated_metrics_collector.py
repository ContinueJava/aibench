import json
import time
from typing import Dict, List, Optional

import numpy as np

from genai_bench.logging import init_logger
from genai_bench.metrics.metrics import AggregatedMetrics, RequestLevelMetrics
from genai_bench.protocol import LiveMetricsData
from genai_bench.time_units import TimeUnitConverter

logger = init_logger(__name__)


class AggregatedMetricsCollector:
    """
    A class to collect and aggregate metrics from individual requests.

    Attributes:
        aggregated_metrics (AggregatedMetrics): An instance to store aggregated
            metrics data.
        all_request_metrics (List[RequestLevelMetrics]): A list that contains
            all the single request level metrics.
        _live_metrics_data (Dict): A dictionary that stores the metrics data
            to update lively on the UI.
    """

    def __init__(self):
        self.aggregated_metrics: AggregatedMetrics = AggregatedMetrics()
        self.all_request_metrics: List[RequestLevelMetrics] = []
        self._live_metrics_data: LiveMetricsData = {
            "ttft": [],
            "input_throughput": [],
            "output_throughput": [],
            "output_latency": [],
            "stats": {},
        }
        self._last_update_time = 0.0
        self._update_interval = None

    def add_single_request_metrics(self, metrics: RequestLevelMetrics):
        """Adds metrics from a single request to the aggregated metrics."""
        # Log abnormal metrics with detailed information for diagnostics
        self.filter_metrics(metrics)

        # Store individual request metrics
        self.all_request_metrics.append(metrics)

        # Track error codes frequency directly
        if metrics.error_code:
            if metrics.error_code not in self.aggregated_metrics.error_codes_frequency:
                self.aggregated_metrics.error_codes_frequency[metrics.error_code] = 0
            self.aggregated_metrics.error_codes_frequency[metrics.error_code] += 1
        else:
            self.aggregated_metrics.num_completed_requests += 1

            # Collect live metrics for updating in real-time
            if metrics.ttft is not None:
                self._live_metrics_data["ttft"].append(metrics.ttft)  # type: ignore[union-attr]
            if metrics.input_throughput is not None:
                self._live_metrics_data["input_throughput"].append(  # type: ignore[union-attr]
                    metrics.input_throughput
                )
            if metrics.output_throughput is not None:
                self._live_metrics_data["output_throughput"].append(  # type: ignore[union-attr]
                    metrics.output_throughput
                )
            if metrics.output_latency is not None:
                self._live_metrics_data["output_latency"].append(metrics.output_latency)  # type: ignore[union-attr]

            # Update live metrics aggregation
            if self._update_interval is not None:
                current_time = time.time()
                if current_time - self._last_update_time < self._update_interval:
                    return
                self._last_update_time = current_time
            self._update_live_metrics()

    @staticmethod
    def filter_metrics(metrics: RequestLevelMetrics) -> bool:
        """
        Detects and handles unreliable TPOT/inference_speed values by setting them
        to None.

        When output generation is very short (output_latency < 0.001s),
        TPOT becomes sensitive to timing jitter and network latency.
        Observed problematic cases had output_latency of ~0.0001s
        to 0.0002s, resulting in impossibly high inference speeds
        The 0.001s threshold provides a safety margin while only
        affecting edge cases,
        as normal LLM generation (10-200 tokens/s) takes >0.001s
        for most requests.

        Returns True if filtering was applied, False otherwise
        """
        inference_speed = metrics.output_inference_speed

        # filter silently for short output latency
        # Don't filter for non-streaming tasks where tpot=0 is intentional
        # (e.g., embeddings, image generation set by _reset_output_metrics).
        # `tpot is not None` matters: a single-token response leaves tpot as
        # None on purpose, and `None != 0` used to be True, so those requests
        # logged "Metric may have abnormal inference speed: None tokens/s".
        if (
            metrics.output_latency is not None
            and metrics.output_latency < 0.001
            and metrics.tpot is not None
            and metrics.tpot != 0
        ):
            logger.warning(
                f"Metric may have abnormal inference speed: "
                f"{inference_speed} tokens/s. "
                f"Filtering it out due to very short output latency. "
                f"(tpot={metrics.tpot}, "
                f"num_output_tokens={metrics.num_output_tokens}, "
                f"output_latency={metrics.output_latency})"
            )
            # Set these fields to None so aggregation skips them
            metrics.tpot = None
            metrics.output_inference_speed = None
            return True

        return False

    def _update_live_metrics(self):
        """Calculates live metrics like avg, max, min, and percentiles."""
        for key in self._live_metrics_data:
            if key == "stats":
                continue
            values: List[float] = self._live_metrics_data[key]  # type: ignore
            if values:
                self._live_metrics_data["stats"][key] = {
                    "min": np.min(values).item(),
                    "max": np.max(values).item(),
                    "mean": np.mean(values).item(),
                }

                if key in ["ttft", "output_latency"]:  # Latency fields
                    percentiles = np.percentile(values, [50, 90, 99])
                    self._live_metrics_data["stats"][key].update(
                        {
                            "p50": percentiles[0].item(),
                            "p90": percentiles[1].item(),
                            "p99": percentiles[2].item(),
                        }
                    )

    def aggregate_metrics_data(
        self,
        start_time: float,
        end_time: float,
        warmup_ratio: Optional[float],
        cooldown_ratio: Optional[float],
    ):
        """
        Aggregates collected metrics data over all requests.

        Args:
            start_time (float): The start time of the aggregated metrics data.
            end_time (float): The end time of the aggregated metrics data.
            warmup_ratio (Optional[float]): The portion of initial requests
                to exclude from the aggregation as warmup.
            cooldown_ratio (Optional[float]): The portion of final requests
                to exclude from the aggregation as cooldown.
        """
        if not self.all_request_metrics:
            logger.warning(
                "‼️ No request metrics collected, one possible cause "
                "is that your run time is too short for the server "
                "to finish any requests. Skipping collecting "
                "aggregated metrics for this run."
            )
            return

        # Calculate statistical aggregates for each metric
        filtered_keys: List[str] = [
            key
            for key in RequestLevelMetrics.model_fields
            if key not in {"error_code", "error_message"}
        ]

        num_collected_requests = len(self.all_request_metrics)

        warmup_number = 0
        if warmup_ratio:
            warmup_number = int(num_collected_requests * warmup_ratio)
            logger.info(
                f"Filtering out first {warmup_number}/{num_collected_requests} "
                f"warmup requests."
            )

        cooldown_number = 0
        if cooldown_ratio:
            cooldown_number = int(num_collected_requests * cooldown_ratio)
            logger.info(
                f"Filtering out last {cooldown_number}/{num_collected_requests} "
                f"cooldown requests."
            )

        # Requests that survive the warmup/cooldown trimming. This window is used
        # both for the statistics below and for the duration that throughput and
        # RPS are divided by, so the numerator (token sums) and the denominator
        # (duration) always cover the same portion of the run.
        aggregated_requests = self.all_request_metrics[
            warmup_number : num_collected_requests - cooldown_number
        ]
        first_aggregated_index = warmup_number
        if not aggregated_requests:
            logger.warning(
                "‼️ Warmup and cooldown ratios filtered out all collected "
                "requests. Skipping collecting aggregated metrics for this run."
            )
            return

        # Metrics that ended up without a single usable sample in the window.
        # Reported once at the end of the loop instead of one warning per key.
        empty_metrics: List[str] = []

        for key in filtered_keys:
            # Extract the list of values for this metric from all requests
            values: List[float] = []
            for i, metrics in enumerate(
                aggregated_requests, start=first_aggregated_index
            ):
                # Skip adding the value when the request metric has error
                if metrics.error_code:
                    continue
                value = getattr(metrics, key)
                if value is None:
                    logger.info(f"{i}th request has NoneType value in metric {key}.")
                    continue
                values.append(value)

            # Validate that all values are valid for processing
            if not values:
                if key in RequestLevelMetrics.AUDIO_METRICS_FIELDS:
                    continue
                # Every request in the aggregation window failed (or the metric
                # is undefined for all of them, e.g. tpot for single-token
                # responses). A run where the server returned nothing but
                # errors is a legitimate -- and important -- result: it must be
                # reported through error_rate instead of crashing the whole
                # benchmark, so leave this metric's stats empty.
                empty_metrics.append(key)
                continue

            percentiles = np.percentile(values, [25, 50, 75, 90, 95, 99])
            stat_field = getattr(self.aggregated_metrics.stats, key)
            stat_field.min = np.min(values).item()
            stat_field.max = np.max(values).item()
            stat_field.mean = np.mean(values).item()
            stat_field.stddev = np.std(values).item()
            stat_field.sum = np.sum(values).item()
            stat_field.p25 = percentiles[0].item()
            stat_field.p50 = percentiles[1].item()
            stat_field.p75 = percentiles[2].item()
            stat_field.p90 = percentiles[3].item()
            stat_field.p95 = percentiles[4].item()
            stat_field.p99 = percentiles[5].item()

        # The warmup/cooldown ratios are applied over the collected requests, so
        # the time window has to be trimmed in the same way before it is used as
        # the denominator of throughput and RPS. Requests are assumed to complete
        # at a roughly constant rate, which is the same assumption the request
        # count based trimming above already relies on.
        full_run_duration = end_time - start_time
        aggregated_duration = full_run_duration * (
            len(aggregated_requests) / num_collected_requests
        )
        self.aggregated_metrics.run_duration = aggregated_duration

        # Handle None values safely
        num_output_tokens_sum = self.aggregated_metrics.stats.num_output_tokens.sum or 0
        num_input_tokens_sum = self.aggregated_metrics.stats.num_input_tokens.sum or 0
        total_tokens_sum = self.aggregated_metrics.stats.total_tokens.sum or 0

        self.aggregated_metrics.mean_output_throughput_tokens_per_s = (
            num_output_tokens_sum / aggregated_duration
            if aggregated_duration > 0
            else 0
        )

        self.aggregated_metrics.mean_input_throughput_tokens_per_s = (
            num_input_tokens_sum / aggregated_duration if aggregated_duration > 0 else 0
        )

        self.aggregated_metrics.mean_total_tokens_throughput_tokens_per_s = (
            total_tokens_sum / aggregated_duration if aggregated_duration > 0 else 0
        )

        # Only the requests inside the warmup/cooldown window are reported, so the
        # request counters are recomputed for that window as well instead of
        # keeping the totals collected over the whole run.
        window_error_codes: Dict[int, int] = {}
        window_completed_requests = 0
        for metrics in aggregated_requests:
            if metrics.error_code:
                window_error_codes[metrics.error_code] = (
                    window_error_codes.get(metrics.error_code, 0) + 1
                )
            else:
                window_completed_requests += 1

        self.aggregated_metrics.error_codes_frequency = window_error_codes
        self.aggregated_metrics.num_completed_requests = window_completed_requests
        self.aggregated_metrics.num_error_requests = sum(window_error_codes.values())
        self.aggregated_metrics.num_requests = (
            self.aggregated_metrics.num_completed_requests
            + self.aggregated_metrics.num_error_requests
        )

        # Calculate error rate. num_requests has to be known before it is used
        # as the denominator, otherwise this is always 0.
        self.aggregated_metrics.error_rate = (
            self.aggregated_metrics.num_error_requests
            / self.aggregated_metrics.num_requests
            if self.aggregated_metrics.num_requests > 0
            else 0
        )

        if self.aggregated_metrics.error_rate >= 0.5:
            logger.warning(
                f"‼️ Error rate {self.aggregated_metrics.error_rate}"
                f" for current run is greater than 0.5! Please "
                f"check logs from genai-bench and server!"
            )

        # Calculate requests per second over the same trimmed window
        self.aggregated_metrics.requests_per_second = (
            (self.aggregated_metrics.num_completed_requests / aggregated_duration)
            if aggregated_duration > 0
            else 0
        )

        if empty_metrics:
            logger.warning(
                f"‼️ No values found for these metrics in the aggregation "
                f"window: {', '.join(empty_metrics)}. Their statistics are left "
                f"empty for this run, which happens when every request failed "
                f"or when the metric is undefined for all of them (e.g. tpot "
                f"for single-token responses)."
            )

    def set_run_metadata(
        self,
        iteration: int,
        scenario_str: str,
        metrics_refresh_interval: Optional[float] = None,
        iteration_type: str = "num_concurrency",
        num_concurrency: Optional[int] = None,
    ):
        """
        Set metadata for the current run.

        ``iteration`` is the value of the *varying* dimension named by
        ``iteration_type``; the other dimension keeps its own value. For
        batch_size iteration the concurrent user count is fixed for the whole
        series, so ``num_concurrency`` records what was actually used instead
        of leaving the field at its default.
        """
        setattr(self.aggregated_metrics, iteration_type, iteration)
        if iteration_type == "batch_size" and num_concurrency:
            self.aggregated_metrics.num_concurrency = int(num_concurrency)
        self.aggregated_metrics.scenario = scenario_str
        self.aggregated_metrics.iteration_type = iteration_type
        if metrics_refresh_interval is not None:
            if not isinstance(metrics_refresh_interval, (int, float)):
                raise TypeError("metrics_refresh_interval must be a number")
            if metrics_refresh_interval < 0:
                raise ValueError("metrics_refresh_interval must be non-negative")
            self._update_interval = float(metrics_refresh_interval)
            logger.info(
                f"Live metrics refresh interval set to: {self._update_interval}s"
            )

    def clear(self):
        """Clear the metrics to prepare for the next experiment run."""
        self.aggregated_metrics = AggregatedMetrics()
        self.all_request_metrics = []
        self._live_metrics_data = {
            "ttft": [],
            "input_throughput": [],
            "output_throughput": [],
            "output_latency": [],
            "stats": {},
        }
        self._last_update_time = 0.0
        self._update_interval = None

    def save(self, file_path: str, metrics_time_unit: str = "s"):
        if not self.all_request_metrics:
            # Returning silently left the experiment folder without any result
            # file, which the report tooling later reported as "no run data".
            logger.warning(
                f"‼️ No request metrics were collected for this run, so "
                f"{file_path} was not written. This happens when no request "
                f"completed (for example because --warmup-ratio/--cooldown-ratio "
                f"filtered out every request, or the run was too short)."
            )
            return

        # Convert aggregated metrics to the specified time unit
        aggregated_dict = TimeUnitConverter.convert_metrics_dict(
            self.aggregated_metrics.model_dump(), metrics_time_unit
        )

        # Convert individual request metrics to the specified time unit
        individual_dicts = TimeUnitConverter.convert_metrics_list(
            [metrics.model_dump() for metrics in self.all_request_metrics],
            metrics_time_unit,
        )

        data_to_save = {
            "aggregated_metrics": aggregated_dict,
            "individual_request_metrics": individual_dicts,
            "_time_unit": metrics_time_unit,  # Store metadata for reference
        }
        with open(file_path, "w") as metrics_file:
            json.dump(data_to_save, metrics_file, indent=4)

    def get_live_metrics(self) -> LiveMetricsData:
        """Returns the latest live metrics for use in the UI.

        The lists are copied so the caller cannot mutate the live state that
        the request handlers keep appending to.
        """
        return {
            metric: list(values) if isinstance(values, list) else dict(values)
            for metric, values in self._live_metrics_data.items()
        }

    def get_ui_scatter_plot_metrics(
        self, metrics_time_unit: str = "s"
    ) -> List[float] | None:
        """Returns the plot metrics for use in the UI."""
        mean_ttft = self.aggregated_metrics.stats.ttft.mean
        mean_output_latency = self.aggregated_metrics.stats.output_latency.mean
        if mean_ttft is None or mean_output_latency is None:
            return None

        # Convert time-based metrics to the specified time unit for UI display
        converted_ttft = TimeUnitConverter.convert_value(
            mean_ttft, "s", metrics_time_unit
        )
        converted_output_latency = TimeUnitConverter.convert_value(
            mean_output_latency, "s", metrics_time_unit
        )

        return [
            converted_ttft,
            converted_output_latency,
            self.aggregated_metrics.mean_input_throughput_tokens_per_s,
            self.aggregated_metrics.mean_output_throughput_tokens_per_s,
        ]
