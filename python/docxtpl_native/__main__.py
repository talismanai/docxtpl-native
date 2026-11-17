"""Command-line interface: ``python -m docxtpl_native template.docx data.json output.docx``.

Arguments, checks and messages follow ``python -m docxtpl``; rendering and
saving use this package.
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

from jinja2.exceptions import TemplateError

from . import DocxTemplate

TEMPLATE_ARG = "template_path"
JSON_ARG = "json_path"
OUTPUT_ARG = "output_filename"
OVERWRITE_ARG = "overwrite"
QUIET_ARG = "quiet"


def make_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        usage=f"python -m docxtpl_native [-h] [-o] [-q] {TEMPLATE_ARG} {JSON_ARG} {OUTPUT_ARG}",
        description="Make docx file from existing template docx and json data.",
    )
    parser.add_argument(TEMPLATE_ARG, type=str, help="The path to the template docx file.")
    parser.add_argument(JSON_ARG, type=str, help="The path to the json file with the data.")
    parser.add_argument(OUTPUT_ARG, type=str, help="The filename to save the generated docx.")
    parser.add_argument("-" + OVERWRITE_ARG[0], "--" + OVERWRITE_ARG, action="store_true",
                        help="If output file already exists, overwrites without asking for confirmation")
    parser.add_argument("-" + QUIET_ARG[0], "--" + QUIET_ARG, action="store_true",
                        help="Do not display unnecessary messages")
    return parser


def get_args(parser: argparse.ArgumentParser) -> dict[str, Any]:
    try:
        return vars(parser.parse_args())
    # Argument errors exit with code 2; --help exits with code 0.
    except SystemExit as error:
        if error.code == 0:
            raise SystemExit from None
        raise RuntimeError(f"Correct usage is:\n{parser.usage}") from None


def check_exists_ask_overwrite(arg_value: str, overwrite: bool) -> bool:
    if os.path.exists(arg_value) and not overwrite:
        try:
            message = f"File {arg_value} already exists, would you like to overwrite the existing file? (y/n)"
            if input(message).lower() == "y":
                return True
            raise OSError
        except OSError:
            raise RuntimeError(f"File {arg_value} already exists, please choose a different name.") from None
    return True


def is_argument_valid(arg_name: str, arg_value: Any, overwrite: bool) -> bool:
    if arg_name == TEMPLATE_ARG:
        return os.path.isfile(arg_value) and arg_value.endswith(".docx")
    if arg_name == JSON_ARG:
        return os.path.isfile(arg_value) and arg_value.endswith(".json")
    if arg_name == OUTPUT_ARG:
        return arg_value.endswith(".docx") and check_exists_ask_overwrite(arg_value, overwrite)
    if arg_name in (OVERWRITE_ARG, QUIET_ARG):
        return arg_value in (True, False)
    return False


def validate_all_args(parsed_args: dict[str, Any]) -> None:
    overwrite = parsed_args[OVERWRITE_ARG]
    for arg_name, arg_value in parsed_args.items():
        if not is_argument_valid(arg_name, arg_value, overwrite):
            raise RuntimeError(f'The specified {arg_name} "{arg_value}" is not valid.')


def get_json_data(json_path: str) -> Any:
    with open(json_path) as file:
        try:
            return json.load(file)
        except json.JSONDecodeError as error:
            print(f"There was an error on line {error.lineno}, column {error.colno} while trying "
                  f"to parse file {json_path}")
            raise RuntimeError("Failed to get json data.") from None


def make_docxtemplate(template_path: str) -> DocxTemplate:
    try:
        return DocxTemplate(template_path)
    except TemplateError:
        raise RuntimeError("Could not create docx template.") from None


def render_docx(doc: DocxTemplate, json_data: Any) -> DocxTemplate:
    try:
        doc.render(json_data)
        return doc
    except TemplateError:
        raise RuntimeError("An error ocurred while trying to render the docx") from None


def save_file(doc: DocxTemplate, parsed_args: dict[str, Any]) -> None:
    try:
        output_path = parsed_args[OUTPUT_ARG]
        doc.save(output_path)
        if not parsed_args[QUIET_ARG]:
            print(f"Document successfully generated and saved at {output_path}")
    except OSError as error:
        print(f"{error.strerror}. Could not save file {error.filename}.")
        raise RuntimeError("Failed to save file.") from None


def main() -> None:
    parser = make_arg_parser()
    parsed_args = get_args(parser)
    try:
        validate_all_args(parsed_args)
        json_data = get_json_data(os.path.abspath(parsed_args[JSON_ARG]))
        doc = make_docxtemplate(os.path.abspath(parsed_args[TEMPLATE_ARG]))
        doc = render_docx(doc, json_data)
        save_file(doc, parsed_args)
    except RuntimeError as error:
        print("Error: " + str(error))
    finally:
        if not parsed_args[QUIET_ARG]:
            print("Exiting program!")


if __name__ == "__main__":
    main()
