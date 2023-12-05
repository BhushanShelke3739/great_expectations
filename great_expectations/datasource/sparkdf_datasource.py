from __future__ import annotations

import datetime
import logging
import uuid
from typing import TYPE_CHECKING, Optional

from great_expectations.compatibility import pyspark
from great_expectations.core.batch import Batch, BatchMarkers
from great_expectations.core.util import get_or_create_spark_session
from great_expectations.dataset import SparkDFDataset
from great_expectations.datasource.datasource import LegacyDatasource
from great_expectations.exceptions import BatchKwargsError

if TYPE_CHECKING:
    from great_expectations.data_context import DataContext

logger = logging.getLogger(__name__)


class SparkDFDatasource(LegacyDatasource):
    """The SparkDFDatasource produces SparkDFDatasets and supports generators capable of interacting with local
        filesystem (the default subdir_reader batch kwargs  generator) and databricks notebooks.

        Accepted Batch Kwargs:
            - PathBatchKwargs ("path" or "s3" keys)
            - InMemoryBatchKwargs ("dataset" key)

    --ge-feature-maturity-info--

        id: datasource_hdfs_spark
            title: Datasource - HDFS
            icon:
            short_description: HDFS
            description: Use HDFS as an external datasource in conjunction with Spark.
            how_to_guide_url:
            maturity: Experimental
            maturity_details:
                api_stability: Stable
                implementation_completeness: Unknown
                unit_test_coverage: Minimal (none)
                integration_infrastructure_test_coverage: Minimal (none)
                documentation_completeness:  Minimal (none)
                bug_risk: Unknown

    --ge-feature-maturity-info--
    """

    recognized_batch_parameters = {
        "reader_method",
        "reader_options",
        "limit",
        "dataset_options",
    }

    @staticmethod
    def build_configuration(
        spark_config: Optional[dict] = None,
        persist: bool = True,
        **kwargs,
    ) -> dict:
        """
        Build a full configuration object for a datasource.

        Args:
            spark_config: dictionary of key-value pairs to pass to the spark builder
            persist: Whether to persist the Spark Dataframe or not.
            **kwargs: Additional kwargs to be part of the datasource constructor's initialization

        Returns:
            A complete datasource configuration.

        """
        configuration = kwargs or {}
        configuration.update(
            {
                "spark_config": spark_config or {},
                "persist": persist,
            }
        )
        return configuration

    def __init__(
        self,
        name="default",
        data_context: Optional[DataContext] = None,
        spark_config: Optional[dict] = None,
        persist: bool = True,
        **kwargs,
    ) -> None:
        """Build a new SparkDFDatasource instance.

        Args:
            name: the name of this datasource
            data_context: the DataContext to which this datasource is connected
            spark_config: dictionary of key-value pairs to be set on the spark session builder
            persist: Whether to persist the Spark Dataframe or not.
            **kwargs: Additional
        """
        configuration: dict = SparkDFDatasource.build_configuration(
            spark_config,
            persist,
            **kwargs,
        )
        super().__init__(
            name,
            data_context=data_context,
            **configuration,
        )

        self.spark: pyspark.SparkSession = get_or_create_spark_session(
            spark_config=spark_config or {},
        )

    def process_batch_parameters(
        self, reader_method=None, reader_options=None, limit=None, dataset_options=None
    ):
        batch_kwargs = super().process_batch_parameters(
            limit=limit,
            dataset_options=dataset_options,
        )

        # Apply globally-configured reader options first
        if reader_options:
            # Then update with any locally-specified reader options
            if not batch_kwargs.get("reader_options"):
                batch_kwargs["reader_options"] = {}
            batch_kwargs["reader_options"].update(reader_options)

        if reader_method is not None:
            batch_kwargs["reader_method"] = reader_method

        return batch_kwargs

    def get_batch(self, batch_kwargs, batch_parameters=None):
        """class-private implementation of get_data_asset"""
        if self.spark is None:
            logger.error("No spark session available")
            return None

        reader_options = batch_kwargs.get("reader_options", {})

        # We need to build batch_markers to be used with the DataFrame
        batch_markers = BatchMarkers(
            {
                "ge_load_time": datetime.datetime.now(datetime.timezone.utc).strftime(
                    "%Y%m%dT%H%M%S.%fZ"
                )
            }
        )

        if "path" in batch_kwargs:
            path = batch_kwargs["path"]
            reader_method = batch_kwargs.get("reader_method")
            reader = self.spark.read

            for option in reader_options.items():
                reader = reader.option(*option)
            reader_fn = self._get_reader_fn(reader, reader_method, path)
            df = reader_fn(path)

        elif "query" in batch_kwargs:
            df = self.spark.sql(batch_kwargs["query"])

        elif "dataset" in batch_kwargs and (
            (
                pyspark.DataFrame  # type: ignore[truthy-function]
                and isinstance(batch_kwargs["dataset"], pyspark.DataFrame)
            )
            or isinstance(batch_kwargs["dataset"], SparkDFDataset)
        ):
            df = batch_kwargs.get("dataset")
            # We don't want to store the actual dataframe in kwargs; copy the remaining batch_kwargs
            batch_kwargs = {k: batch_kwargs[k] for k in batch_kwargs if k != "dataset"}
            if isinstance(df, SparkDFDataset):
                # Grab just the spark_df reference, since we want to override everything else
                df = df.spark_df
            # Record this in the kwargs *and* the id
            batch_kwargs["SparkDFRef"] = True
            batch_kwargs["ge_batch_id"] = str(uuid.uuid1())

        else:
            raise BatchKwargsError(
                "Unrecognized batch_kwargs for spark_source", batch_kwargs
            )

        if "limit" in batch_kwargs:
            df = df.limit(batch_kwargs["limit"])

        return Batch(
            datasource_name=self.name,
            batch_kwargs=batch_kwargs,
            data=df,
            batch_parameters=batch_parameters,
            batch_markers=batch_markers,
            data_context=self._data_context,
        )

    @staticmethod
    def guess_reader_method_from_path(path: str):
        path = path.lower()
        if path.endswith(".csv") or path.endswith(".tsv"):
            return {"reader_method": "csv"}
        elif (
            path.endswith(".parquet") or path.endswith(".parq") or path.endswith(".pqt")
        ):
            return {"reader_method": "parquet"}

        raise BatchKwargsError(
            f"Unable to determine reader method from path: {path}",
            {"path": path},
        )

    def _get_reader_fn(self, reader, reader_method=None, path=None):
        """Static helper for providing reader_fn

        Args:
            reader: the base spark reader to use; this should have had reader_options applied already
            reader_method: the name of the reader_method to use, if specified
            path (str): the path to use to guess reader_method if it was not specified

        Returns:
            ReaderMethod to use for the filepath

        """
        if reader_method is None and path is None:
            raise BatchKwargsError(
                "Unable to determine spark reader function without reader_method or path.",
                {"reader_method": reader_method},
            )

        if reader_method is None:
            reader_method = self.guess_reader_method_from_path(path=path)[
                "reader_method"
            ]

        try:
            if reader_method.lower() in ["delta", "avro"]:
                return reader.format(reader_method.lower()).load

            return getattr(reader, reader_method)
        except AttributeError:
            raise BatchKwargsError(
                f"Unable to find reader_method {reader_method} in spark.",
                {"reader_method": reader_method},
            )
