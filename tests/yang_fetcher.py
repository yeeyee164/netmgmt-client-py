# -*- encoding: utf-8 -*-
"""
yang_fetcher.py

Utility for dynamically gathering YANG data models and their recursive dependency closures
from live NETCONF targets via RFC 6022 <get-schema> and caching them to vendor directories and SchemaDB.

NOTE: It may be included as an official feature in the future if needed.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import os
import re
import logging
from pathlib import Path
from typing import Optional, List, Set, Tuple, Sequence, Dict, Any

try:
    from ncclient import manager
except ImportError:
    manager = None

from core.schema_db import SchemaDB
from core.schema_resolver import SchemaResolver

logger = logging.getLogger(__name__)


def _resolve_yang_dir(raw_path_str: str, device: str, base_dir: Path) -> Path:
    """Resolves YANG vendor directory from configured relative or absolute path."""
    p = Path(raw_path_str) if raw_path_str else Path(device)
    if p.is_absolute() and p.exists():
        return p

    candidates = [
        base_dir / "composite" / p,     # relative to tests/composite/
        base_dir / p,                   # relative to tests/
        base_dir / "yang" / p.name,     # e.g. tests/yang/srlinux or tests/yang/ocnos_derived
        base_dir / "yang" / device,     # fallback to tests/yang/<device>
        base_dir.parent / p,            # relative to repo root
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()

    # If none exist yet, fallback to canonical tests/yang/<dir_name>
    target_name = p.name if p.name and p.name != ".." else device
    return (base_dir / "yang" / target_name).resolve()


@dataclass(kw_only=True)
class YangTestSuite:
    """Immutable test suite configuration for a network device."""
    targets: Dict[str, str]  # "netconf": 'address', "gnmi": 'address', ...
    modules: List[str]       # list of YANG modules
    fetcher: YangSchemaFetcher
    yang_dir: Path
    credentials: Dict[str, str] = field(default_factory=dict)
    device: str = ""

    @property
    def netconf_target(self) -> Optional[str]:
        return self.targets.get("netconf")

    @property
    def gnmi_target(self) -> Optional[str]:
        return self.targets.get("gnmi")

    @property
    def username(self) -> str:
        return self.credentials.get("user", "admin")

    @property
    def password(self) -> str:
        return self.credentials.get("password", "")


def yang_test_suite_builder(device: str, config: str | Path = "") -> Optional[YangTestSuite]:
    """
    Creates an instantiated `YangTestSuite` via json(`config`) file.
    """
    import json

    tests_dir = Path(__file__).resolve().parent

    candidate_paths = [
        Path(config) if config else None,
        tests_dir / "testing_env.json",
        tests_dir.parent / "testing_env.json",
        Path.cwd() / "testing_env.json",
        Path.cwd() / "tests" / "testing_env.json",
    ]

    test_profile_path: Optional[Path] = None
    for cp in candidate_paths:
        if cp and cp.is_file():
            test_profile_path = cp.resolve()
            break

    if not test_profile_path:
        logger.warning("Could not find testing_env.json configuration file.")
        return None

    with open(test_profile_path, "r", encoding="utf-8") as f:
        res = json.load(f)

    profile = res.get(device)
    if profile is None:
        logger.warning(f"Device profile '{device}' not found in {test_profile_path}")
        return None

    yang_p = profile.get("yang", {})
    cred = profile.get("credentials", {})
    protocols = profile.get("protocols", {})

    raw_yang_path = yang_p.get("yang_path") or yang_p.get("paths") or yang_p.get("path") or ""
    yang_dir = _resolve_yang_dir(raw_yang_path, device, tests_dir)

    timeout = yang_p.get("timeout", 15)
    netconf_target = protocols.get("netconf", "")

    fetcher = YangSchemaFetcher(
        target=netconf_target,
        username=cred.get("user", "admin"),
        password=cred.get("password", ""),
        output_dir=yang_dir,
        timeout=timeout,
    )

    return YangTestSuite(
        targets=protocols,
        modules=yang_p.get("yang_module", []),
        fetcher=fetcher,
        yang_dir=yang_dir,
        credentials=cred,
        device=device,
    )

class YangSchemaFetcher:
    """
    Fetches YANG models and submodules from a NETCONF target and saves them to disk and/or SchemaDB.
    """

    def __init__(
        self,
        target: str,
        username: str = "admin",
        password: str = "",
        output_dir: Optional[Path] = None,
        schema_db: Optional[SchemaDB] = None,
        timeout: int = 15,
    ):
        self.target = target
        self.username = username
        self.password = password
        self.timeout = timeout
        self.output_dir = Path(output_dir).resolve() if output_dir else None
        if self.output_dir:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        self.schema_db = schema_db
        self._session = None

    def _get_connection(self):
        if manager is None:
            raise RuntimeError("ncclient is required to fetch schemas via NETCONF.")
        if ":" in self.target:
            host, port = self.target.split(":", 1)
            port_num = int(port)
        else:
            host = self.target
            port_num = 830

        return manager.connect(
            host=host,
            port=port_num,
            username=self.username,
            password=self.password,
            hostkey_verify=False,
            timeout=self.timeout,
        )

    @staticmethod
    def extract_schema_text(reply: Any) -> Optional[str]:
        """Extracts plain YANG text from an ncclient get_schema reply object or XML string."""
        # Check if reply has direct .data string (e.g. ncclient GetSchemaReply)
        data_attr = getattr(reply, "data", None)
        if isinstance(data_attr, str) and ("module " in data_attr or "submodule " in data_attr):
            return data_attr.strip()

        raw_xml = getattr(reply, "xml", None)
        if raw_xml is None:
            raw_xml = str(reply)

        # Pattern 1: match <data ...> ... </data>
        match = re.search(r"<(?:\w+:)?data\b[^>]*>(.*?)</(?:\w+:)?data>", raw_xml, re.DOTALL)
        if match:
            text = match.group(1).strip()
            if text.startswith("<![CDATA[") and text.endswith("]]>"):
                return text[9:-3].strip()
            return text

        # Pattern 2: CDATA block directly
        cdata_match = re.search(r"<!\[CDATA\[(.*?)\]\]>", raw_xml, re.DOTALL)
        if cdata_match:
            return cdata_match.group(1).strip()

        # Pattern 3: module or submodule root text
        if "module " in raw_xml or "submodule " in raw_xml:
            text = re.sub(r"<\?[^>]+\?>", "", raw_xml)
            text = re.sub(r"<[^>]+>", "", text)
            return text.strip()

        return str(reply).strip()

    def find_local_file(self, module_name: str, revision: Optional[str] = None) -> Optional[Path]:
        """Checks if the requested module file already exists in output_dir."""
        if not self.output_dir or not self.output_dir.is_dir():
            return None
        if revision:
            exact = self.output_dir / f"{module_name}@{revision}.yang"
            if exact.is_file():
                return exact
        base = self.output_dir / f"{module_name}.yang"
        if base.is_file():
            if revision is None:
                return base
            content = base.read_text(encoding="utf-8")
            if f"revision {revision}" in content or f'revision "{revision}"' in content or f"revision '{revision}'" in content:
                return base
        matches = sorted(self.output_dir.glob(f"{module_name}@*.yang"), reverse=True)
        if matches and revision is None:
            return matches[0]
        return None

    def fetch_module_content(self, module_name: str, revision: Optional[str] = None) -> Optional[str]:
        """Executes <get-schema> against the target device."""
        logger.info(f"[YangFetcher] Fetching '{module_name}' (rev: {revision}) from {self.target}...")
        with self._get_connection() as m:
            try:
                res = m.get_schema(identifier=module_name, version=revision or None)
                return self.extract_schema_text(res)
            except Exception as e:
                # If specific revision failed, retry without revision
                if revision:
                    try:
                        res = m.get_schema(identifier=module_name)
                        return self.extract_schema_text(res)
                    except Exception:
                        pass
                logger.warning(f"[YangFetcher] get_schema failed for '{module_name}' on {self.target}: {e}")
                return None

    def fetch_and_save(
        self,
        module_name: str,
        revision: Optional[str] = None,
        recursive: bool = True,
        visited: Optional[Set[Tuple[str, Optional[str]]]] = None,
    ) -> Path:
        """
        Ensures a module (and its dependencies if recursive=True) is saved into output_dir
        and indexed in SchemaDB.
        """
        if visited is None:
            visited = set()
        key = (module_name, revision)
        if key in visited:
            local = self.find_local_file(module_name, revision)
            if local:
                return local
        visited.add(key)

        local_path = self.find_local_file(module_name, revision)
        content: Optional[str] = None

        if local_path and local_path.is_file():
            content = local_path.read_text(encoding="utf-8")
        else:
            content = self.fetch_module_content(module_name, revision)
            if not content:
                raise RuntimeError(
                    f"Failed to fetch YANG module '{module_name}' (rev: {revision}) from target {self.target}"
                )

            # Determine filename
            rev_str = revision.strip() if revision else ""
            if not rev_str:
                # Extract primary revision from content if present
                rev_match = re.search(r'revision\s+["\']?([0-9]{4}-[0-9]{2}-[0-9]{2})["\']?', content)
                if rev_match:
                    rev_str = rev_match.group(1)

            filename = f"{module_name}@{rev_str}.yang" if rev_str else f"{module_name}.yang"
            target_path = self.output_dir / filename if self.output_dir else None

            if target_path:
                target_path.write_text(content, encoding="utf-8")
                local_path = target_path
                logger.info(f"[YangFetcher] Saved '{module_name}' to {local_path}")

        # Index in SchemaDB if available
        if self.schema_db:
            is_sub = bool(re.search(r"^\s*submodule\s+", content, re.MULTILINE))
            self.schema_db.store_module_content(
                name=module_name,
                content=content,
                revision=revision,
                kind="submodule" if is_sub else "module",
                source="netconf_get_schema",
                source_uri=f"netconf://{self.target}",
                verify_with_libyang=False,
            )

        if recursive and content:
            deps = SchemaResolver.extract_dependencies(content)
            for dep_name, dep_rev, dep_kind in deps:
                try:
                    self.fetch_and_save(
                        module_name=dep_name,
                        revision=dep_rev,
                        recursive=True,
                        visited=visited,
                    )
                except Exception as e:
                    logger.warning(
                        f"[YangFetcher] Could not resolve dependency '{dep_name}' for '{module_name}': {e}"
                    )

        return local_path