from mcp.server.fastmcp import FastMCP, Context
import json
import os
import sys
from typing import Dict, List, AsyncIterator, Optional
import time
from contextlib import asynccontextmanager
from pathlib import Path
from dotenv import load_dotenv

from db_context import DatabaseContext

# Load environment variables from .env file
load_dotenv()

ORACLE_CONNECTION_STRING = os.getenv('ORACLE_CONNECTION_STRING')
TARGET_SCHEMA = os.getenv('TARGET_SCHEMA')  # Optional schema override
CACHE_DIR = os.getenv('CACHE_DIR', '.cache')
USE_THICK_MODE = os.getenv('THICK_MODE', '').lower() in ('true', '1', 'yes')  # Convert string to boolean

@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[DatabaseContext]:
    """Manage application lifecycle and ensure DatabaseContext is properly initialized"""
    print("App Lifespan initialising", file=sys.stderr)
    connection_string = ORACLE_CONNECTION_STRING
    if not connection_string:
        raise ValueError("ORACLE_CONNECTION_STRING environment variable is required. Set it in .env file or environment.")
    
    cache_dir = Path(CACHE_DIR)
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    db_context = DatabaseContext(
        connection_string=connection_string,
        cache_path=cache_dir / 'schema_cache.json',
        target_schema=TARGET_SCHEMA,
        use_thick_mode=USE_THICK_MODE  # Pass the thick mode setting
    )
    
    try:
        # Initialize cache on startup
        print("Initialising database cache...", file=sys.stderr)
        await db_context.initialize()
        print("Cache ready!", file=sys.stderr)
        yield db_context
    finally:
        # Ensure proper cleanup of database resources
        print("Closing database connections...", file=sys.stderr)
        await db_context.close()
        print("Database connections closed", file=sys.stderr)

# Initialize FastMCP server
mcp = FastMCP("oracle", lifespan=app_lifespan)
print("FastMCP server initialized", file=sys.stderr)

@mcp.tool()
async def get_table_schema(table_name: str, ctx: Context) -> str:
    """
    Get schema information for a table including columns, data types, and nullability.
    Shows table structure, relationships, and constraints. Useful for query writing and data modeling.
    
    Args:
        table_name: Table name (case-insensitive). Must be exact match, no wildcards.
                   Use search_tables_schema for pattern matching.
    
    Returns:
        Table schema details including columns and relationships.
        Error message if table not found.
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    table_info = await db_context.get_schema_info(table_name)
    
    if not table_info:
        return f"Table '{table_name}' not found in the schema."
    
    # Delegate formatting to the TableInfo model
    return table_info.format_schema()

@mcp.tool()
async def rebuild_schema_cache(ctx: Context) -> str:
    """
    Force rebuild of database schema cache. Use when schema changes occur after startup.
    Warning: Resource-intensive operation, may take several minutes for large databases.
    
    Returns:
        Success message with table count or error message if rebuild fails.
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    try:
        await db_context.rebuild_cache()
        cache_size = len(db_context.schema_manager.cache.all_table_names) if db_context.schema_manager.cache else 0
        return f"Schema cache rebuilt successfully. Indexed {cache_size} tables."
    except Exception as e:
        return f"Failed to rebuild schema cache: {str(e)}"

@mcp.tool()
async def get_tables_schema(table_names: List[str], ctx: Context) -> str:
    """
    Get schema information for multiple tables in a single query.
    More efficient than multiple single-table queries. Use for analyzing
    multiple tables' relationships or designing multi-table queries.
    
    Args:
        table_names: List of table names (case-insensitive). Each must be exact.
    
    Returns:
        Schema details for all requested tables, including columns and relationships.
        Includes error messages for tables not found.
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    results = []
    
    for table_name in table_names:
        table_info = await db_context.get_schema_info(table_name)
        if not table_info:
            results.append(f"\nTable '{table_name}' not found in the schema.")
            continue
        
        # Delegate formatting to the TableInfo model
        results.append(table_info.format_schema())
    
    return "\n".join(results)

@mcp.tool()
async def search_tables_schema(search_term: str, ctx: Context) -> str:
    """
    Search for tables by name and return their schema information. Supports multiple search terms
    (comma or space separated) with case-insensitive matching. Useful for exploring tables when
    you know part of the name or domain (e.g., 'customer', 'order').
    
    Results limited to 20 tables for performance. Use more specific terms if too many matches found.
    Matches substrings anywhere in table names (e.g., 'cust' matches 'CUSTOMERS', 'customer_data').
    
    Args:
        search_term: Search strings (case-insensitive), separated by commas or spaces.
                    Each term treated as separate search (OR condition).
    
    Returns:
        Schema information for matching tables (up to 20), including columns and relationships.
        Error message if no matches found.
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    
    # Split search term by commas and whitespace and remove empty strings
    search_terms = [term.strip() for term in search_term.replace(',', ' ').split()]
    search_terms = [term for term in search_terms if term]
    
    if not search_terms:
        return "No valid search terms provided"
    
    # Track all matching tables without duplicates
    matching_tables = set()
    
    # Search for each term
    for term in search_terms:
        tables = await db_context.search_tables(term, limit=20)
        matching_tables.update(tables)
    
    # Convert back to list and limit to 20 results
    matching_tables = list(matching_tables)
    total_matches = len(matching_tables)
    limited_tables = matching_tables[:20]
    
    if not matching_tables:
        return f"No tables found matching any of these terms: {', '.join(search_terms)}"
    
    if total_matches > 20:
        results = [f"Found {total_matches} tables matching terms ({', '.join(search_terms)}). Returning the first 20 for performance reasons:"]
    else:
        results = [f"Found {total_matches} tables matching terms ({', '.join(search_terms)}):"]
    
    matching_tables = limited_tables
    
    # Now load the schema for each matching table
    for table_name in matching_tables:
        table_info = await db_context.get_schema_info(table_name)
        if not table_info:
            continue
        
        # Delegate formatting to the TableInfo model
        results.append(table_info.format_schema())
    
    return "\n".join(results)

@mcp.tool()
async def get_database_vendor_info(ctx: Context) -> str:
    """
    Returns database vendor type and version information.
    Helps identify available SQL features and syntax for the connected database.
    Shows vendor name, version, schema context, and additional details when available.
    
    Returns:
        Database vendor info including type, version, and schema.
        Returns error message if query fails.
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    
    try:
        db_info = await db_context.get_database_info()
        
        if not db_info:
            return "Could not retrieve database vendor information."
        
        result = [f"Database Vendor: {db_info.get('vendor', 'Unknown')}"]
        result.append(f"Version: {db_info.get('version', 'Unknown')}")
        if "schema" in db_info:
            result.append(f"Schema: {db_info['schema']}")
        
        if "additional_info" in db_info and db_info["additional_info"]:
            result.append("\nAdditional Version Information:")
            for info in db_info["additional_info"]:
                result.append(f"- {info}")
                
        if "error" in db_info:
            result.append(f"\nError: {db_info['error']}")
            
        return "\n".join(result)
    except Exception as e:
        return f"Error retrieving database vendor information: {str(e)}"

@mcp.tool()
async def search_columns(search_term: str, ctx: Context) -> str:
    """
    Find columns across all tables matching the search term.
    Case-insensitive search, limited to 50 matches for performance.
    Shows table name, column name, data type, and nullability.
    
    Args:
        search_term: Column name to search (e.g., 'customer_id', 'date').
                    No wildcards/regex support.
    
    Returns:
        Matching columns with their details, grouped by table.
        Error message if none found.
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    
    try:
        matching_columns = await db_context.search_columns(search_term, limit=50)
        
        if not matching_columns:
            return f"No columns found matching '{search_term}'"
        
        results = [f"Found columns matching '{search_term}' in {len(matching_columns)} tables:"]
        
        for table_name, columns in matching_columns.items():
            results.append(f"\nTable: {table_name}")
            results.append("Matching columns:")
            for col in columns:
                nullable = "NULL" if col["nullable"] else "NOT NULL"
                results.append(f"  - {col['name']}: {col['type']} {nullable}")
        
        return "\n".join(results)
    except Exception as e:
        return f"Error searching columns: {str(e)}"

@mcp.tool()
async def get_pl_sql_objects(object_type: str, name_pattern: Optional[str], ctx: Context) -> str:
    """
    Get information about PL/SQL objects (procedures, functions, packages, etc.) in the database.
    Useful for discovering and analyzing database code objects. Supports filtering by object type
    and name pattern.
    
    Includes object names, status, owner info, and timestamps. Results may be limited for
    performance reasons.
    
    Args:
        object_type: Type of object (PROCEDURE, FUNCTION, PACKAGE, etc.). Auto-converted to uppercase.
        name_pattern: Optional filter pattern (case-insensitive, supports % wildcards).
                     Example: "CUSTOMER%" finds objects starting with "CUSTOMER".
    
    Returns:
        Matching PL/SQL objects with their details. Error message if none found.
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    
    try:
        objects = await db_context.get_pl_sql_objects(object_type.upper(), name_pattern)
        
        if not objects:
            pattern_msg = f" matching '{name_pattern}'" if name_pattern else ""
            return f"No {object_type.upper()} objects found{pattern_msg}"
        
        results = [f"Found {len(objects)} {object_type.upper()} objects:"]
        
        for obj in objects:
            results.append(f"\n{obj['type']}: {obj['name']}")
            if 'owner' in obj:
                results.append(f"Owner: {obj['owner']}")
            if 'status' in obj:
                results.append(f"Status: {obj['status']}")
            if 'created' in obj:
                results.append(f"Created: {obj['created']}")
            if 'last_modified' in obj:
                results.append(f"Last Modified: {obj['last_modified']}")
        
        return "\n".join(results)
    except Exception as e:
        return f"Error retrieving PL/SQL objects: {str(e)}"

@mcp.tool()
async def get_object_source(object_type: str, object_name: str, ctx: Context) -> str:
    """
    Get source code for PL/SQL objects (procedures, functions, packages, etc.).
    Shows complete source with original formatting and comments.
    
    Args:
        object_type: Type of object (PROCEDURE, FUNCTION, etc.)
        object_name: Name of object (case-insensitive)
    
    Returns:
        Complete source code or error if not found/no permissions.
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    
    try:
        source = await db_context.get_object_source(object_type.upper(), object_name.upper())
        
        if not source:
            return f"No source found for {object_type} {object_name}"
        
        return f"Source for {object_type} {object_name}:\n\n{source}"
    except Exception as e:
        return f"Error retrieving object source: {str(e)}"

@mcp.tool()
async def get_table_constraints(table_name: str, ctx: Context) -> str:
    """
    Get table constraints (primary keys, foreign keys, unique, check constraints).
    Shows data integrity rules and relationships. Important for understanding valid
    data operations and table relationships.
    
    Returns constraint names, types, affected columns, and additional details:
    - Foreign keys: referenced table and columns
    - Check constraints: validation conditions
    - Unique/Primary: column combinations
    
    Args:
        table_name: Table name (case-insensitive). Must be exact match.
    
    Returns:
        All constraints with detailed information. Error if none found.
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    
    try:
        constraints = await db_context.get_table_constraints(table_name)
        
        if not constraints:
            return f"No constraints found for table '{table_name}'"
        
        results = [f"Constraints for table '{table_name}':"]
        
        for constraint in constraints:
            constraint_type = constraint.get('type', 'UNKNOWN')
            name = constraint.get('name', 'UNNAMED')
            
            results.append(f"\n{constraint_type} Constraint: {name}")
            
            if 'columns' in constraint:
                results.append(f"Columns: {', '.join(constraint['columns'])}")
                
            if constraint_type == 'FOREIGN KEY' and 'references' in constraint:
                ref = constraint['references']
                results.append(f"References: {ref['table']}({', '.join(ref['columns'])})")
                
            if 'condition' in constraint:
                results.append(f"Condition: {constraint['condition']}")
        
        return "\n".join(results)
    except Exception as e:
        return f"Error retrieving constraints: {str(e)}"

@mcp.tool()
async def get_table_indexes(table_name: str, ctx: Context) -> str:
    """
    Get indexes for a table to understand and optimize query performance.
    Shows index names, columns, uniqueness, and status information.
    
    Args:
        table_name: Table name (case-insensitive). Must be exact match.
    
    Returns:
        Table's indexes with column info and properties.
        Error if none found or access denied.
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    
    try:
        indexes = await db_context.get_table_indexes(table_name)
        
        if not indexes:
            return f"No indexes found for table '{table_name}'"
        
        results = [f"Indexes for table '{table_name}':"]
        
        for idx in indexes:
            idx_type = "UNIQUE " if idx.get('unique', False) else ""
            results.append(f"\n{idx_type}Index: {idx['name']}")
            results.append(f"Columns: {', '.join(idx['columns'])}")
            
            if 'tablespace' in idx:
                results.append(f"Tablespace: {idx['tablespace']}")
                
            if 'status' in idx:
                results.append(f"Status: {idx['status']}")
        
        return "\n".join(results)
    except Exception as e:
        return f"Error retrieving indexes: {str(e)}"

@mcp.tool()
async def get_dependent_objects(object_name: str, ctx: Context) -> str:
    """
    Find objects that depend on the specified object (usage references).
    Important for impact analysis before modifying database objects.
    Shows views, procedures, triggers, and other dependent objects.
    
    Args:
        object_name: Object name (case-insensitive, auto-uppercase).
                    Must be exact, no wildcards.
    
    Returns:
        List of dependent objects with types and owners.
        Error if none found.
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    
    try:
        dependencies = await db_context.get_dependent_objects(object_name.upper())
        
        if not dependencies:
            return f"No objects found that depend on '{object_name}'"
        
        results = [f"Objects that depend on '{object_name}':"]
        
        for dep in dependencies:
            results.append(f"\n{dep['type']}: {dep['name']}")
            if 'owner' in dep:
                results.append(f"Owner: {dep['owner']}")
        
        return "\n".join(results)
    except Exception as e:
        return f"Error retrieving dependencies: {str(e)}"

@mcp.tool()
async def get_user_defined_types(type_pattern: Optional[str], ctx: Context) -> str:
    """
    Get info about user-defined types (object types, nested tables, VARRAYs).
    Shows type structure, attributes, and categories.
    
    Args:
        type_pattern: Optional filter (case-insensitive, supports % wildcards)
                     Example: "CUSTOMER%" finds CUSTOMER_TYPE, etc.
    
    Returns:
        Type details including attributes and categories.
        Error if none found.
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    
    try:
        types = await db_context.get_user_defined_types(type_pattern)
        
        if not types:
            pattern_msg = f" matching '{type_pattern}'" if type_pattern else ""
            return f"No user-defined types found{pattern_msg}"
        
        results = [f"User-defined types:"]
        
        for typ in types:
            results.append(f"\nType: {typ['name']}")
            results.append(f"Type category: {typ['type_category']}")
            if 'owner' in typ:
                results.append(f"Owner: {typ['owner']}")
            if 'attributes' in typ and typ['attributes']:
                results.append("Attributes:")
                for attr in typ['attributes']:
                    results.append(f"  - {attr['name']}: {attr['type']}")
        
        return "\n".join(results)
    except Exception as e:
        return f"Error retrieving user-defined types: {str(e)}"

@mcp.tool()
async def get_related_tables(table_name: str, ctx: Context) -> str:
    """
    Find tables connected through foreign keys (incoming/outgoing).
    Shows complete relationship map for table navigation and JOIN planning.
    Includes both referenced tables and tables referencing this one.
    
    Args:
        table_name: Table to analyze (case-insensitive). Must be exact.
    
    Returns:
        Related tables in both directions with relationship types.
        Error if none found.
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    
    try:
        related = await db_context.get_related_tables(table_name)
        
        if not related['referenced_tables'] and not related['referencing_tables']:
            return f"No related tables found for '{table_name}'"
        
        results = [f"Tables related to '{table_name}':"]
        
        if related['referenced_tables']:
            results.append("\nTables referenced by this table (outgoing foreign keys):")
            for table in related['referenced_tables']:
                results.append(f"  - {table}")
        
        if related['referencing_tables']:
            results.append("\nTables that reference this table (incoming foreign keys):")
            for table in related['referencing_tables']:
                results.append(f"  - {table}")
        
        return "\n".join(results)
        
    except Exception as e:
        return f"Error getting related tables: {str(e)}"

@mcp.tool()
async def execute_query(query: str, ctx: Context) -> str:
    """
    Execute a SQL query and return the results in a formatted table.
    Use this tool to run SELECT queries against the database. The query must be a valid Oracle SQL statement.
    
    Args:
        query: The SQL query to execute. Must be a valid SELECT statement.
              Example: "SELECT * FROM employees WHERE department_id = 10"
    
    Returns:
        Query results in a formatted table structure, including column headers and row data.
        Returns an error message if the query is invalid or execution fails.
    """
    db_context: DatabaseContext = ctx.request_context.lifespan_context
    
    try:
        result = await db_context.execute_query(query)
        
        if result.get("status") == "error":
            return f"Error executing query: {result.get('error', 'Unknown error')}"
        
        # Format the results as a table
        output = []
        
        # Add column headers
        if result["columns"]:
            output.append("| " + " | ".join(result["columns"]) + " |")
            output.append("|" + "|".join(["-" * (len(col) + 2) for col in result["columns"]]) + "|")
        
        # Add rows
        for row in result["rows"]:
            formatted_row = [str(val) if val is not None else "NULL" for val in row]
            output.append("| " + " | ".join(formatted_row) + " |")
        
        # Add summary
        output.append(f"\nTotal rows: {result['rowCount']}")
        
        return "\n".join(output)
        
    except Exception as e:
        return f"Error executing query: {str(e)}"

if __name__ == "__main__":
    mcp.run()
