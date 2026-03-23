"""
Unit tests for the miner's word extraction and splitting logic.
Run with:  pytest tests/test_miner.py -v
"""

import sys
import os

# Allow importing miner.py from the parent directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "miner"))

from miner import split_identifier, extract_words_python, extract_words_java


# ══════════════════════════════════════════════════════════════════════════════
#  split_identifier
# ══════════════════════════════════════════════════════════════════════════════

class TestSplitIdentifier:
    """Tests for the identifier-splitting helper."""

    def test_snake_case(self):
        assert split_identifier("make_response") == ["make", "response"]

    def test_snake_case_with_underscores(self):
        assert split_identifier("get_file_content") == ["get", "file", "content"]

    def test_camel_case(self):
        assert split_identifier("retainAll") == ["retain", "all"]

    def test_pascal_case(self):
        assert split_identifier("GetUserName") == ["get", "user", "name"]

    def test_dunder_method(self):
        assert split_identifier("__init__") == ["init"]

    def test_single_underscore_prefix(self):
        assert split_identifier("_private_method") == ["private", "method"]

    def test_all_caps_acronym_in_camel(self):
        # getHTTPStatus → get, http, status
        result = split_identifier("getHTTPStatus")
        assert "get" in result
        assert "http" in result
        assert "status" in result

    def test_mixed_snake_and_camel(self):
        # get_HTTPResponse → get, http, response
        result = split_identifier("get_HTTPResponse")
        assert "get" in result

    def test_filters_short_words(self):
        # single-letter segments should be filtered
        result = split_identifier("toX")
        assert "x" not in result

    def test_empty_string(self):
        assert split_identifier("") == []

    def test_only_underscores(self):
        assert split_identifier("___") == []

    def test_screaming_snake(self):
        result = split_identifier("MAX_RETRY_COUNT")
        assert result == ["max", "retry", "count"]


# ══════════════════════════════════════════════════════════════════════════════
#  extract_words_python
# ══════════════════════════════════════════════════════════════════════════════

class TestExtractWordsPython:
    """Tests for Python AST-based extraction."""

    def test_simple_function(self):
        src = "def make_response(arg): pass"
        words = extract_words_python(src)
        assert "make" in words
        assert "response" in words

    def test_async_function(self):
        src = "async def fetch_data(url): pass"
        words = extract_words_python(src)
        assert "fetch" in words
        assert "data" in words

    def test_method_inside_class(self):
        src = """
class MyClass:
    def get_value(self):
        return 42
    def set_value(self, v):
        pass
"""
        words = extract_words_python(src)
        assert "get" in words
        assert "value" in words
        assert "set" in words

    def test_dunder_method(self):
        src = "class C:\n    def __init__(self): pass"
        words = extract_words_python(src)
        assert "init" in words

    def test_nested_function(self):
        src = """
def outer_func():
    def inner_helper():
        pass
"""
        words = extract_words_python(src)
        assert "outer" in words
        assert "func" in words
        assert "inner" in words
        assert "helper" in words

    def test_invalid_syntax_returns_empty(self):
        src = "def broken(:"
        assert extract_words_python(src) == []

    def test_no_functions_returns_empty(self):
        src = "x = 1 + 2\nprint(x)"
        assert extract_words_python(src) == []

    def test_type_annotated_function(self):
        src = 'def make_response(*args) -> "Response": ...'
        words = extract_words_python(src)
        assert "make" in words
        assert "response" in words


# ══════════════════════════════════════════════════════════════════════════════
#  extract_words_java
# ══════════════════════════════════════════════════════════════════════════════

class TestExtractWordsJava:
    """Tests for the Java regex-based extractor."""

    def test_public_method(self):
        src = "public boolean retainAll(Collection<?> c) {"
        words = extract_words_java(src)
        assert "retain" in words
        assert "all" in words

    def test_private_method(self):
        src = "private void processRequest(HttpRequest req) {"
        words = extract_words_java(src)
        assert "process" in words
        assert "request" in words

    def test_static_method(self):
        src = "public static String formatDate(long ts) {"
        words = extract_words_java(src)
        assert "format" in words
        assert "date" in words

    def test_method_with_throws(self):
        src = "protected void readFile(Path p) throws IOException {"
        words = extract_words_java(src)
        assert "read" in words
        assert "file" in words

    def test_keywords_excluded(self):
        src = "if (x > 0) { for (int i = 0; i < 10; i++) {"
        words = extract_words_java(src)
        assert "if" not in words
        assert "for" not in words

    def test_multiple_methods(self):
        src = """
public class Example {
    public void startServer() {}
    private boolean isConnected() {}
    protected String buildResponse() {}
}
"""
        words = extract_words_java(src)
        assert "start" in words
        assert "server" in words
        assert "connected" in words
        assert "build" in words
        assert "response" in words

    def test_getter_setter(self):
        src = """
    public String getName() { return name; }
    public void setName(String name) { this.name = name; }
"""
        words = extract_words_java(src)
        assert "get" in words
        assert "name" in words
        assert "set" in words