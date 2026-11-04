# -*- encoding: utf-8 -*-
import sys
import json

from modules.formatter import GNMIFormatter, NETCONFFormatter
from config import(
    OutputConfig,
    OutputType,
    OutputFormat
)

class OutputHandler:
    """
    Consumes telemetry data and writes it to the configured destination.
    It will be used by the central manager class.

    Incoming data should be typed 'dictionary'. It should contain such fields:
    * 'data': base64 formatted response result
    * 'protocol': which protocol used for response
    * 'rpc': supported RPC for 'protocol'
    * 'format': output format

    NOTE: It's not the encoding rule defined at protocol.
    """

    def __init__(self, config:OutputConfig):
        self.name = config.name
        self.file_type = config.output_type
        self.format = config.format

        # Determine the output stream
        if self.file_type == OutputType.STDOUT:
            self.stream = sys.stdout
        elif self.file_type == OutputType.STDERR:
            self.stream = sys.stderr
        elif self.file_type == OutputType.FILE:
            self.stream = open(str(config.path), 'a')
            print(f"[Output] Created file sink: {self.file_type}")
        else:
            # default case is STDOUT
            self.stream = sys.stdout

    def write(self, message):
        """Formats and writes the message to the defined stream"""
        if isinstance(message, list):
            for item in message:
                self.write(item)
            return

        raw_data = message.get('data')
        protocol = message.get('protocol')
        sub_name = message.get('subscription_name', '')

        # Instantiate protocol formatter
        if protocol == 'netconf':
            self.formatter = NETCONFFormatter()
        else: # default is gNMI
            self.formatter = GNMIFormatter()

        if self.format in ['json', 'xml']:
            meta = {
                'source': message.get('target'),
            }
            if message.get('op_id'):
                meta['op_id'] = message.get('op_id')

            if sub_name != '':
                meta['subscription_name'] = sub_name

            rpc = message.get('rpc')
            meta = {k: v for k, v in meta.items() if v is not None}

            if self.format == 'json':
                formatted_data = self.formatter.format_json(raw_data, rpc=rpc, meta=meta)
                output_str = json.dumps(formatted_data, indent=2)
            elif self.format == 'xml':
                formatted_data = self.formatter.format_xml(raw_data, rpc=rpc, meta=meta)
                output_str = formatted_data
        elif self.format == 'text':
            formatted_data = self.formatter.format_text(raw_data)
            rpc = message.get('rpc')
            op_id = message.get('op_id')
            op_str = f"{rpc} | op_id: {op_id}" if (rpc and op_id) else (rpc or "")
            if op_str:
                output_str = f"\n--- [Operation: {op_str}] ---\n{formatted_data}"
            else:
                output_str = f"\n{formatted_data}"
        else:
            output_str = str(message)

        self.stream.write(output_str + '\n')
        self.stream.flush()
    
    def close(self):
        """Safely closes file handles if they aren't standard system streams"""
        if self.stream not in (sys.stdout, sys.stderr):
            self.stream.close()
