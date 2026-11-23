# -*- encoding: utf-8 -*-
import pytest

from cli.cmd import parse_args
from config.model import OutputFormat, OutputType

pytestmark = pytest.mark.unit


class TestCLIConfigBuilderOutputs:
    def test_cli_output_type_file_uses_output_file_arg(self):
        parsed = parse_args([
            "-t", "10.0.0.1:57401",
            "--output-type", "file",
            "--output-file", "/tmp/gnmi_out.json",
            "--output-format", "json",
            "gnmi", "capability",
        ])
        assert len(parsed.outputs) == 1
        out = parsed.outputs[0]
        assert out.output_type == OutputType.FILE
        assert out.format == OutputFormat.JSON
        assert out.path == "/tmp/gnmi_out.json"

    def test_cli_output_type_file_without_output_file_raises_value_error(self):
        with pytest.raises(ValueError, match="path must be specified"):
            parse_args([
                "-t", "10.0.0.1:57401",
                "--output-type", "file",
                "gnmi", "capability",
            ])
