import ast
from ast import NodeVisitor
from collections import OrderedDict
from dataclasses import dataclass, field
import argparse
import os
from pathlib import Path
from datetime import datetime
import re
import sys
import textwrap
import glob

__version__ = "1.6"

comment_pattern = re.compile(r"#(.*)$")

# Pattern for the % string operator
python_format = re.compile(r'''
    %                     # Start of the specifier
    (\(([\w]+)\))?        # Mapping key (optional)
    ([-#0\ +])?           # Conversion flags (optional)
    (\*|\d+)?             # Minimum field width (optional)
    (\.(\*|\d+))?         # Precision (optional)
    ([hlL])?              # Length modifier (optional)
    ([diouxXeEfFgGcrs%])  # Conversion type
''', re.VERBOSE)

escapes = {
    "\\": r"\\",
    "\t": r"\t",
    "\r": r"\r",
    "\n": r"\n",
    "\"": r"\"",
}

pot_header = """\
# Translations template for {project_name}.
# Copyright (C) {year} {copyright_holder}
# This file is distributed under the same license as the PROJECT project.
# FIRST AUTHOR <EMAIL@ADDRESS>, {year}.
#
#, fuzzy
msgid ""
msgstr ""
"Project-Id-Version: {project_name} {project_version}\\n"
"Report-Msgid-Bugs-To: {msgid_bugs_address}\\n"
"POT-Creation-Date: 2023-05-02 13:48+0200\\n"
"PO-Revision-Date: YEAR-MO-DA HO:MI+ZONE\\n"
"Last-Translator: FULL NAME <EMAIL@ADDRESS>\\n"
"Language-Team: LANGUAGE <LL@li.org>\\n"
"MIME-Version: 1.0\\n"
"Content-Type: text/plain; charset={charset}\\n"
"Content-Transfer-Encoding: 8bit\\n"
"Generated-By: pygettext {version}\\n"
"""


class GettextFormatError(Exception):
    pass


@dataclass
class Message:
    filename: str
    lineno: int
    msgid: str
    msgctx: str = None
    msgid_plural: str = None
    is_python_format: bool = False  # TODO: use a flags field
    comments: list[str] = field(default_factory=list)


def _is_string_const(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def extract_arguments(node, keyword_spec):
    arguments = {}
    for key, idx in keyword_spec.items():
        if idx >= len(node.args):
            raise GettextFormatError("Not enough arguments in function call")

        arg = node.args[idx]
        if not _is_string_const(arg):
            raise GettextFormatError("Argument must be a string constant")

        arguments[key] = arg.value
    return arguments


def parse_keywords(strings):
    keywords = {}
    for string in strings:
        funcname, *arguments = string.split(":")
        if not arguments:
            spec = {"msgid": 0}
        else:
            arguments = arguments[0].split(",")
            spec = {}
            for arg in arguments:
                is_ctx = False
                if arg[-1] == "c":
                    is_ctx = True
                    arg = arg[:-1]

                arg = int(arg) - 1
                if is_ctx:
                    spec["msgctx"] = arg
                elif "msgid" not in spec:
                    spec["msgid"] = arg
                else:
                    spec["msgid_plural"] = arg
        keywords[funcname] = spec
    return keywords


marking_keywords = parse_keywords([
    "_",
    "gettext",
    "dgettext:2",
    "ngettext:1,2",
    "dngettext:2,3",
    "pgettext:1c,2",
    "dpgettext:2c,3",
    "npgettext:1c,2,3",
    "dnpgettext:2c,3,4",
])


def _get_funcname(node):
    if isinstance(node.func, ast.Name):
        name = node.func.id
    elif isinstance(node.func, ast.Attribute):
        name = node.func.attr
    else:
        return None

    if name not in marking_keywords:
        return None
    return name


def dedup_messages(messages):
    combined = OrderedDict()
    for msg in messages:
        key = (msg.msgctx, msg.msgid)
        if key not in combined:
            combined[key] = [msg]
        else:
            combined[key].append(msg)
    return combined


def is_python_format(msg):
    return bool(python_format.search(msg))


class GettextVisitor(NodeVisitor):
    def __init__(self, opts, source, filename):
        super().__init__()
        self.opts = opts
        self.source_lines = source.split("\n")
        self.filename = filename
        self.messages = []

    def _get_comments(self, node):
        comments = []
        # lineno is 1-indexed
        lineno = node.lineno - 2
        while lineno >= 0:
            line = self.source_lines[lineno]
            if match := comment_pattern.match(line):
                comments.append(match.group(1))
            else:
                break
            lineno -= 1

        comments.reverse()
        return comments

    def visit_Call(self, node):
        opts = self.opts
        if (funcname := _get_funcname(node)) and funcname in opts.marking_keywords:
            try:
                keyword_spec = opts.marking_keywords[funcname]
                arguments = extract_arguments(node, keyword_spec)
            except GettextFormatError as exc:
                if opts.verbose:
                    print(f"skipping {self.filename}:{node.lineno}:{node.col_offset + 1}: {exc}", file=sys.stderr)
            else:
                comments = self._get_comments(node) if opts.add_comments else []
                python_format = is_python_format(arguments.get("msgid", ""))
                python_format |= is_python_format(arguments.get("msgid_plural", ""))

                message = Message(self.filename, node.lineno,
                                  arguments.get("msgid"), arguments.get("msgctx"), arguments.get("msgid_plural"),
                                  python_format, comments)
                self.messages.append(message)
        self.generic_visit(node)


def format_comments(comments):
    return "\n".join(f"#. {comment}" for comment in comments)


def format_file_occurrences(messages, opts):
    lines = [f"#: {messages[0].filename}:{messages[0].lineno}"]
    for msg in messages[1:]:
        occurrence = f"{msg.filename}:{msg.lineno}"
        if len(f"{lines[-1]} {occurrence}") <= opts.width:
            lines[-1] = f"{lines[-1]} {occurrence}"
        else:
            lines.append(f"#: {occurrence}")
    return "\n".join(lines)


def format_flag(flag):
    return f"#, {flag}"


def normalize(string):
    normalized = ""
    for c in string:
        normalized += escapes.get(c, c)
    return normalized


def format_string(prefix, string, opts):
    # TODO: cleanup
    lines = string.split("\n")
    if len(lines) == 1:
        line = normalize(lines[0])
        if len(f'{prefix} "{line}"') <= opts.width:
            return f'{prefix} "{line}"'
        else:
            output = textwrap.wrap(line, width=opts.width - 2, expand_tabs=False, replace_whitespace=False, drop_whitespace=False)
            return 'msgid ""\n' + "\n".join([f'"{line}"' for line in output])

    output = []
    for i, line in enumerate(lines):
        if i == len(lines) - 1:
            line = normalize(line)
        else:
            line = normalize(line + "\n")
        output += textwrap.wrap(line, width=opts.width - 2, expand_tabs=False, replace_whitespace=False, drop_whitespace=False)

    return "\n".join([f'"{line}"' for line in output])


def format_msgctx(string, opts):
    return format_string("msgctx", string, opts)


def format_msgid(string, opts):
    return format_string("msgid", string, opts)


def format_plural(string, opts):
    return format_string("msgid_plural", string, opts)


def format_entry(messages, opts):
    msgctx = messages[0].msgctx
    msgid = messages[0].msgid
    msgid_plural = messages[0].msgid_plural
    python_format = messages[0].is_python_format
    output = ""

    if opts.add_comments is not None:
        comments = []
        for msg in messages:
            if comment := format_comments(msg.comments):
                comments.append(comment)
        if comments:
            output += "\n".join(comments)
            output += "\n"

    if not opts.no_location:
        output += format_file_occurrences(messages, opts)
        output += "\n"

    if python_format:
        output += format_flag("python-format")
        output += "\n"

    if msgctx is not None:
        output += format_msgctx(msgctx, opts)
        output += "\n"

    output += format_msgid(msgid, opts)
    output += "\n"

    if msgid_plural is None:
        output += 'msgstr ""'
    else:
        output += format_plural(msgid_plural, opts)
        output += '\nmsgstr[0] ""'
        output += '\nmsgstr[1] ""'
    return output


def format_file_header(opts):
    year = datetime.now().year
    return pot_header.format(**vars(opts), year=year, version=__version__)


def collect_files(include_patterns, exclude_patterns):
    files = set()
    for pattern in include_patterns:
        path = Path(pattern).expanduser()
        if path.is_file():
            files.add(pattern)
        elif path.is_dir():
            files |= set((f for f in path.iterdir() if f.is_file()))
        else:
            files |= set(glob.glob(str(path), recursive=True))

    cwd = Path.cwd()
    files = [Path(os.path.relpath(file, cwd)) for file in files]

    def matches_exclude(file):
        return any(file.match(pattern) for pattern in exclude_patterns)

    return sorted([file for file in files if not matches_exclude(file)])


def extract_file(path, opts):
    if opts.verbose:
        print(f"extracting messages from {path}", file=sys.stderr)

    with open(path, encoding=opts.charset) as f:
        source = f.read()

    tree = ast.parse(source)
    visitor = GettextVisitor(opts, source, path)
    visitor.visit(tree)
    return visitor.messages


def extract_all_files(opts):
    files = collect_files(opts.input_patterns, opts.exclude_patterns)
    messages = []
    for fname in files:
        messages += extract_file(fname, opts)

    deduped = dedup_messages(messages)
    file_contents = format_file_header(opts)
    file_contents += "\n"

    for messages in deduped.values():
        file_contents += format_entry(messages, opts)
        file_contents += "\n\n"

    if opts.verbose:
        print(f"writing messages to {opts.output}", file=sys.stderr)

    if opts.output == "-":
        print(file_contents)
    else:
        with open(opts.output, "w", encoding=opts.charset) as f:
            f.write(file_contents)


if __name__ == "__main__":
    parser = argparse.ArgumentParser("pygettext")

    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--no-location", help="do not generate '#. filename:lineno' comments",
                        action="store_true")
    parser.add_argument("-o", "--output", help="name of the output file")
    parser.add_argument("--charset", help="charset of the output file, defaults to 'utf-8'",
                        type=str, default="utf-8")
    parser.add_argument("-w", "--width", help="maximum line width of the output",
                        type=int, default=80)
    parser.add_argument("--no-wrap", help="do not wrap lines",
                        action="store_true")
    parser.add_argument("-v", "--verbose", help="print status messages",
                        action="store_true")
    parser.add_argument("-c", "--add-comments", help="increase output verbosity",
                        type=str)
    parser.add_argument("--no-default-keywords", help="do not use any default marking keywords",
                        action="store_true")
    parser.add_argument("-k", "--keyword", help="additional marking keywords to look for",
                        nargs="*", action="append", dest="marking_keywords", default=[])
    parser.add_argument("--exclude-patterns", nargs="*", type=str, default=[],
                        help="ignore files matching these patterns")
    parser.add_argument("--project-name", type=str, help="project name", default="PROJECT")
    parser.add_argument("--project-version", type=str, help="project version", default="VERSION")
    parser.add_argument("--copyright-holder", type=str, help="copyright holder", default="ORGANIZATION")
    parser.add_argument("--msgid-bugs-address", type=str, help="set bug report address", default="EMAIL@ADDRESS")
    parser.add_argument("input_patterns", nargs="+", type=str, help="input patterns")

    args = parser.parse_args()
    args.marking_keywords = parse_keywords(args.marking_keywords) | marking_keywords

    extract_all_files(args)
