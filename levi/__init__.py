import re
from typing import Optional, List
from deltalake import DeltaTable
import datetime
import numpy as np
import pyarrow.compute as pc 


def skipped_stats(delta_table, filters):
    df = delta_table.get_add_actions(flatten=True).to_pandas()
    total_files = len(df)
    total_bytes = df["size_bytes"].sum()
    res = df.query(filters_to_sql(filters))
    num_files_skipped = total_files - len(res)
    num_bytes_skipped = total_bytes - res["size_bytes"].sum()
    return {
        "num_files": total_files,
        "num_files_skipped": num_files_skipped,
        "num_bytes_skipped": num_bytes_skipped
    }


def filters_to_sql(filters):
    res = []
    for filter in filters:
        res.append(filter_to_sql(filter))
    return " and ".join(res)


def filter_to_sql(filter):
    col, operator, val = filter
    if operator == "=":
        return f"(`min.{col}` <= {val} and `max.{col}` >= {val})"
    elif operator == "<":
        return f"(`min.{col}` < {val})"
    elif operator == "<=":
        return f"(`min.{col}` <= {val})"
    elif operator == ">":
        return f"(`max.{col}` > {val})"
    elif operator == ">=":
        return f"(`max.{col}` >= {val})"
    else:
        raise ValueError(f"{filter} cannot be parsed.")


def latest_version(delta_table: DeltaTable):
    return delta_table.version()


def delta_file_sizes(delta_table: DeltaTable, boundaries=None):
    if boundaries is None:
        boundaries = ["<1mb", "1mb-500mb", "500mb-1gb", "1gb-2gb", ">2gb"]
    df = delta_table.get_add_actions(flatten=True).to_pandas()
    res = {}
    for boundary in boundaries:
        min, max = boundary_parser(boundary)
        count = len(df[df['size_bytes'].between(min, max)])
        res[f"num_files_{boundary}"] = count
    return res


def str_to_bytes(s):
    kilobyte = 1_000
    megabyte = 1_000_000
    gigabyte = 1_000_000_000
    terabyte = 1_000_000_000_000
    parts = re.split('(\d+)', s)
    num = int(parts[1])
    units = parts[2]
    if units == "b":
        return num
    elif units == "kb":
        return num * kilobyte
    elif units == "mb":
        return num * megabyte
    elif units == "gb":
        return num * gigabyte
    elif units == "tb":
        return num * terabyte
    else:
        raise ValueError(
            f"{s} cannot be parsed.  Valid units are b, kb, mb, gb, or tb.  45b, 8kb, and 4gb are valid examples.")


def boundary_parser(boundary):
    # TODO: Figure out the proper way to represent this large value
    ten_tb = 10_000_000_000_000
    # Supports <, <=, >, >=, and between which is indicated with a dash
    if boundary.startswith("<="):
        return 0, str_to_bytes(boundary.replace("<=", ""))
    elif boundary.startswith("<"):
        return 0, str_to_bytes(boundary.replace("<=", "")) - 1
    elif boundary.startswith(">="):
        return str_to_bytes(boundary.replace("<=", "")), ten_tb
    elif boundary.startswith(">"):
        return str_to_bytes(boundary.replace("<=", "")) + 1, ten_tb
    elif "-" in boundary:
        first, second = boundary.split("-")
        return str_to_bytes(first), str_to_bytes(second)
    else:
        raise ValueError(
            f"{boundary} cannot be parsed.  Valid prefixes are <, <=, >, and >=.  Two values can be delimited with a dash.  Valid examples include <10kb and 10kb-5gb.")

def updated_partitions(delta_table: DeltaTable, start_time: Optional[datetime.datetime] = None, end_time: Optional[datetime.datetime] = None) -> dict[str, str]:
    add_actions_df = delta_table.get_add_actions().to_pandas()

    if start_time is not None:
        add_actions_df = add_actions_df[add_actions_df["modification_time"] >= np.datetime64(int(start_time.timestamp() * 1e6), "us")]
    if end_time is not None:
        add_actions_df = add_actions_df[add_actions_df["modification_time"] < np.datetime64(int(end_time.timestamp() * 1e6), "us")]

    return add_actions_df.drop_duplicates(subset=["partition_values"])["partition_values"].tolist()

def kill_duplicates(delta_table: DeltaTable, duplication_columns: List[str]):
    """
    <description>

    :param delta_table: <description>
    :type delta_table: DeltaTable
    :param duplication_columns: <description>
    :type duplication_columns: List[str]

    :raises TypeError: Raises type error when input arguments have a invalid type or are empty.
    :raises TypeError: Raises type error when required columns are missing in the provided delta table.
    """

    if not isinstance(delta_table, DeltaTable):
        raise TypeError("An existing delta table must be specified.")

    if not duplication_columns or len(duplication_columns) == 0:
        raise TypeError("Duplication columns must be specified")      

    data_frame = delta_table.to_pyarrow_table()

    # Make sure that all the required columns are present in the provided delta table
    append_data_columns = data_frame.column_names
    for required_column in duplication_columns:
        if required_column not in append_data_columns:
            raise TypeError(
                f"The base table has these columns {append_data_columns!r}, but these columns are required {duplication_columns!r}"
            )
        
    data_frame = (
        data_frame
        .group_by(duplication_columns)
        .aggregate([([], "count_all")])
        .filter(pc.field("count_all") > 1)
    )

    predicate = " AND ".join([f"source.{column} = target.{column}" for column in duplication_columns])

    (
        delta_table
        .merge(
            source=data_frame,
            predicate=predicate,
            source_alias="source",
            target_alias="target")
        .when_matched_delete()
        .execute()
    )