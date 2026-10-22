import os
import importlib
import unittest


class EnvParserTests(unittest.TestCase):
    def test_parse_supports_whitespace_quotes_and_inline_comments(self):
        # Importing here so this test module still loads even when the function
        # does not exist yet (failing-first commit).
        from config import env as config_env  # type: ignore
        parse_dotenv = getattr(config_env, "parse_dotenv")

        content = (
            "# leading comment\n"
            " A = 1 \n"
            "B=\"  2 = two # not comment \"   # trailing comment here\n"
            " C='3#stillvalue' # inline comment\n"
            "D=abc # comment\n"
            "E=\n"
            "F=\"#value#\" # quoted hash stays\n"
        )
        result = parse_dotenv(content)
        self.assertEqual(result["A"], "1")
        self.assertEqual(result["B"], "  2 = two # not comment ")
        self.assertEqual(result["C"], "3#stillvalue")
        self.assertEqual(result["D"], "abc")
        self.assertEqual(result["E"], "")
        self.assertEqual(result["F"], "#value#")

    def test_malformed_line_reports_line_number_only(self):
        from config import env as config_env  # type: ignore
        parse_dotenv = getattr(config_env, "parse_dotenv")
        DotenvParseError = getattr(config_env, "DotenvParseError", ValueError)

        content = "A=1\nBROKEN_LINE\nC='unclosed\n"
        with self.assertRaises(DotenvParseError) as cm:
            parse_dotenv(content)
        msg = str(cm.exception)
        # The first malformed is line 2; the message must not echo the value.
        self.assertIn("line 2", msg)
        self.assertNotIn("BROKEN_LINE", msg)

    def test_env_var_priority_rule_with_setdefault(self):
        from config import env as config_env  # type: ignore
        parse_dotenv = getattr(config_env, "parse_dotenv")
        mapping = parse_dotenv("X=from_file\n")
        try:
            os.environ["X"] = "from_env"
            for k, v in mapping.items():
                os.environ.setdefault(k, v)
            self.assertEqual(os.environ["X"], "from_env")
        finally:
            os.environ.pop("X", None)

    def test_allowed_hosts_list_strips_items(self):
        # Ensure settings import succeeds
        try:
            old_secret = os.environ.get("DJANGO_SECRET_KEY")
            os.environ["DJANGO_SECRET_KEY"] = "dummy-secret-for-tests"
            os.environ["DJANGO_ALLOWED_HOSTS"] = "a.com, b.com ,  c.com ,  ,   d.com , "
            import config.settings as settings  # type: ignore
            importlib.reload(settings)
            self.assertEqual(settings.ALLOWED_HOSTS, ["a.com", "b.com", "c.com", "d.com"])
        finally:
            if old_secret is None:
                os.environ.pop("DJANGO_SECRET_KEY", None)
            else:
                os.environ["DJANGO_SECRET_KEY"] = old_secret
            os.environ.pop("DJANGO_ALLOWED_HOSTS", None)

    def test_inline_hash_without_space_is_literal_and_space_hash_is_comment(self):
        from config import env as config_env  # type: ignore
        parse_dotenv = getattr(config_env, "parse_dotenv")
        content = (
            "API_KEY=abc#def\n"
            "URL=https://x.test/#frag\n"
            "KEEP=one#two#three\n"
            "CUT=abc # comment\n"
            "TAB=xyz\t#tabcomment\n"
        )
        result = parse_dotenv(content)
        self.assertEqual(result["API_KEY"], "abc#def")
        self.assertEqual(result["URL"], "https://x.test/#frag")
        self.assertEqual(result["KEEP"], "one#two#three")
        self.assertEqual(result["CUT"], "abc")
        self.assertEqual(result["TAB"], "xyz")

    def test_unquoted_quotes_are_literal_and_quoted_wrapping_is_stripped(self):
        from config import env as config_env  # type: ignore
        parse_dotenv = getattr(config_env, "parse_dotenv")
        DotenvParseError = getattr(config_env, "DotenvParseError", ValueError)
        content = (
            "COMPANY=O'Brien\n"
            "PASS=ab\"c\n"
            "Q=\"abc\"\n"
            "R='def'\n"
        )
        result = parse_dotenv(content)
        self.assertEqual(result["COMPANY"], "O'Brien")
        self.assertEqual(result["PASS"], "ab\"c")
        self.assertEqual(result["Q"], "abc")
        self.assertEqual(result["R"], "def")
        with self.assertRaises(DotenvParseError):
            parse_dotenv("BAD='abc")  # unmatched opening quote must error


if __name__ == "__main__":
    unittest.main()

