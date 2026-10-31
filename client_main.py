# -*- encoding: utf-8 -*-

from ui.cmd import build_args, config_builder
from ui.runner import CLIApplicationRunner

from modules.logger import setup_logger
import logging
import sys

if __name__ == '__main__':

    args = build_args()
    cfg = config_builder(args)

    # setup logger
    log_level = "DEBUG" if cfg.debug else cfg.log_level
    setup_logger(log_level=log_level, syslog_server=cfg.syslog_server, log_file=cfg.log_file)

    logger = logging.getLogger(__name__)
    logger.info(f"Initializing Client engine...")
    logger.info(f"Initializeing sessions...")
    for sess in cfg.sessions:
        logger.info(f"Target: {sess.target}, Protocol: {sess.protocol}")
    logger.debug(f"Configuration loaded: sessions({len(cfg.sessions)}), requests({len(cfg.requests)})")

    if args.debug:
        print(f"given config:\n{cfg}")

    # run configured client via Application Service Layer runner
    try:
        CLIApplicationRunner.execute(cfg=cfg)
    except Exception as e:
        logger.error(f"Fatal Engine error: {e}", exc_info=True)
        sys.exit(1)