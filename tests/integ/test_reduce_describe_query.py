#
# Copyright (c) 2012-2024 Snowflake Computing Inc. All rights reserved.
#

from typing import List

import pytest

from snowflake.snowpark import DataFrame
from snowflake.snowpark._internal.analyzer.expression import Attribute, Star
from snowflake.snowpark._internal.analyzer.unary_expression import UnresolvedAlias
from snowflake.snowpark._internal.analyzer.unary_plan_node import Project
from snowflake.snowpark._internal.utils import (
    TempObjectType,
    random_name_for_temp_object,
)
from snowflake.snowpark.functions import col, count, lit, seq2, table_function
from snowflake.snowpark.session import (
    _PYTHON_SNOWPARK_REDUCE_DESCRIBE_QUERY_ENABLED,
    Session,
)
from tests.integ.utils.sql_counter import SqlCounter
from tests.utils import IS_IN_STORED_PROC

pytestmark = [
    pytest.mark.skipif(
        "config.getoption('local_testing_mode', default=False)",
        reason="Reducing describe queries is not supported in Local Testing",
    ),
]

param_list = [False, True]
temp_table_name = random_name_for_temp_object(TempObjectType.TABLE)


@pytest.fixture(params=param_list, autouse=True, scope="module")
def setup(request, session):
    is_reduce_describe_query_enabled = session.reduce_describe_query_enabled
    session.reduce_describe_query_enabled = request.param
    session.create_dataframe([[1, 2], [3, 4]], schema=["a", "b"]).write.save_as_table(
        temp_table_name, table_type="temp", mode="overwrite"
    )
    yield
    session.reduce_describe_query_enabled = is_reduce_describe_query_enabled


# Create from SQL
create_from_sql_funcs = [
    lambda session: session.sql("SELECT 1 AS a, 2 AS b"),
    lambda session: session.sql("SELECT 1 AS a, 2 AS b").select("a"),
    lambda session: session.sql("SELECT 1 AS a, 2 AS b").select(
        "a", lit("2").alias("c")
    ),
]

# Create from Values
create_from_values_funcs = [
    lambda session: session.create_dataframe([[1, 2], [3, 4]], schema=["a", "b"]),
    lambda session: session.create_dataframe(
        [[1, 2], [3, 4]], schema=["a", "b"]
    ).select("a"),
    lambda session: session.create_dataframe(
        [[1, 2], [3, 4]], schema=["a", "b"]
    ).select("a", lit("2").alias("c")),
]

# Create from Table
create_from_table_funcs = [
    lambda session: session.table(temp_table_name),
    lambda session: session.table(temp_table_name).select("a"),
    lambda session: session.table(temp_table_name).select("a", lit("2").alias("c")),
]

# Create from SnowflakePlan
create_from_snowflake_plan_funcs = [
    lambda session: session.create_dataframe([[1, 2], [3, 4]], schema=["a", "b"])
    .group_by("a")
    .count(),
    lambda session: session.create_dataframe([[1, 2], [3, 4]], schema=["a", "b"]).join(
        session.sql("SELECT 1 AS a, 2 AS b1"), "a"
    ),
    lambda session: session.create_dataframe(
        [[1, 2], [3, 4]], schema=["a", "b"]
    ).rename({"b": "c"}),
    lambda session: session.range(10).to_df("a"),
    lambda session: session.range(10).select(seq2().as_("a")),  # no flatten
]

# Create from table functions
create_from_table_function_funcs = [
    lambda session: session.create_dataframe(
        [[1, "some string value"]], schema=["a", "b"]
    ).select("a", table_function("split_to_table")("b", lit(" "))),
    lambda session: session.create_dataframe(
        [[1, "some string value"]], schema=["a", "b"]
    )
    .select("a", table_function("split_to_table")("b", lit(" ")))
    .select("a"),
]

# Create from unions
create_from_unions_funcs = [
    lambda session: session.sql("SELECT 1 AS a, 2 AS b").union(
        session.sql("SELECT 3 AS a, 4 AS b")
    ),
    lambda session: session.sql("SELECT 1 AS a, 2 AS b")
    .union(session.sql("SELECT 3 AS a, 4 AS b"))
    .select("a"),
    lambda session: session.sql("SELECT 1 AS a, 2 AS b")
    .union(session.sql("SELECT 3 AS a, 4 AS b"))
    .select("a", lit("2").alias("c")),
]

create_without_select_funcs_expected_describe_count = [
    (create_from_sql_funcs[0], 1),
    (create_from_values_funcs[0], 0),
    (create_from_table_funcs[0], 1),
    (create_from_unions_funcs[0], 0),
]


metadata_no_change_df_ops = [
    lambda df: df.filter(col("a") > 2),
    lambda df: df.filter((col("a") - 2) > 2),
    lambda df: df.sort(col("a").desc()),
    lambda df: df.sort(-col("a")),
    lambda df: df.limit(2),
    lambda df: df.sort(col("a").desc()).limit(2).filter(col("a") > 2),  # no flatten
    lambda df: df.filter(col("a") > 2).sort(col("a").desc()).limit(2),
    lambda df: df.sample(0.5),
    lambda df: df.sample(0.5).filter(col("a") > 2),
    lambda df: df.filter(col("a") > 2).sample(0.5),
]

select_df_ops_expected_quoted_identifiers = [
    (lambda df: df.select("a", col("b")), ['"A"', '"B"']),
    (lambda df: df.select("*", lit(1).as_('"c"')), ['"A"', '"B"', '"c"']),
    (
        lambda df: df.select("*", lit(1).as_('"c"')).select(col("a") == 2),
        ['"(""A"" = 2)"'],
    ),
    (lambda df: df.select("a", (col("b") + 1).as_("b")), ['"A"', '"B"']),
    (lambda df: df.select(count("*")), ['"COUNT(1)"']),
    (
        lambda df: df.select("a", (col("b") + 1).as_("b")).select(
            (col("b") + 1).as_("b"), "a"
        ),
        ['"B"', '"A"'],
    ),
    (
        lambda df: df.select("a", (col("b") + 1).as_("b")).filter(col("a") == 1),
        ['"A"', '"B"'],
    ),
    (
        lambda df: df.filter(col("a") == 1).select("a", (col("b") + 1).as_("b")),
        ['"A"', '"B"'],
    ),
]


def check_attributes_equality(attrs1: List[Attribute], attrs2: List[Attribute]) -> None:
    for attr1, attr2 in zip(attrs1, attrs2):
        assert attr1.name == attr2.name
        assert attr1.datatype == attr2.datatype
        assert attr1.nullable == attr2.nullable


def has_star_in_projection(df: DataFrame) -> bool:
    plan = df._plan.source_plan
    return isinstance(plan, Project) and any(
        isinstance(e, UnresolvedAlias) and isinstance(e.child, Star)
        for e in plan.project_list
    )


@pytest.mark.parametrize(
    "action",
    metadata_no_change_df_ops,
)
@pytest.mark.parametrize(
    "create_df_func",
    create_from_sql_funcs
    + create_from_values_funcs
    + create_from_table_funcs
    + create_from_snowflake_plan_funcs
    + create_from_table_function_funcs
    + create_from_unions_funcs,
)
def test_metadata_no_change(session, action, create_df_func):
    df = create_df_func(session)
    with SqlCounter(query_count=0, describe_count=1):
        attributes = df._plan.attributes
        quoted_identifiers = df._plan.quoted_identifiers

    df = action(df)
    if session.reduce_describe_query_enabled:
        check_attributes_equality(df._plan._metadata.attributes, attributes)
        expected_describe_query_count = 0
    else:
        assert df._plan._metadata.attributes is None
        expected_describe_query_count = 1

    with SqlCounter(query_count=0, describe_count=expected_describe_query_count):
        _ = df.schema
        _ = df.columns
        assert df._plan._metadata.quoted_identifiers is None
        assert df._plan.quoted_identifiers == quoted_identifiers


@pytest.mark.parametrize(
    "action,expected_quoted_identifiers",
    select_df_ops_expected_quoted_identifiers,
)
@pytest.mark.parametrize(
    "create_df_func,expected_describe_query_count",
    create_without_select_funcs_expected_describe_count,
)
def test_select_quoted_identifiers(
    sql_simplifier_enabled,
    session,
    action,
    expected_quoted_identifiers,
    create_df_func,
    expected_describe_query_count,
):
    df = create_df_func(session)

    # if sql simplifier is disabled, there is no describe query
    # because we don't need to get quoted identifiers
    with SqlCounter(
        query_count=0,
        describe_count=expected_describe_query_count if sql_simplifier_enabled else 0,
    ):
        df = action(df)

    # if we select a "*", it can't be inferred when sql simplifier is disabled
    # because no describe query is issued before to get quoted identifiers
    if session.reduce_describe_query_enabled and not has_star_in_projection(df):
        assert df._plan._metadata.quoted_identifiers == expected_quoted_identifiers
        expected_describe_query_count = 0
    else:
        assert df._plan._metadata.quoted_identifiers is None
        expected_describe_query_count = 1

    assert df._plan._metadata.attributes is None
    with SqlCounter(query_count=0, describe_count=expected_describe_query_count):
        quoted_identifiers = df._plan.quoted_identifiers
        assert quoted_identifiers == expected_quoted_identifiers


@pytest.mark.skipif(IS_IN_STORED_PROC, reason="Can't create a session in SP")
def test_reduce_describe_query_enabled_on_session(db_parameters):
    with Session.builder.configs(db_parameters).create() as new_session:
        default_value = new_session.reduce_describe_query_enabled
        new_session.reduce_describe_query_enabled = not default_value
        assert new_session.reduce_describe_query_enabled is not default_value
        new_session.reduce_describe_query_enabled = default_value
        assert new_session.reduce_describe_query_enabled is default_value

        parameters = db_parameters.copy()
        parameters["session_parameters"] = {
            _PYTHON_SNOWPARK_REDUCE_DESCRIBE_QUERY_ENABLED: not default_value
        }
        with Session.builder.configs(parameters).create() as new_session2:
            assert new_session2.reduce_describe_query_enabled is not default_value