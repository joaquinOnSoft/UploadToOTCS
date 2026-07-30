#!/usr/bin/env python3
"""
upload_to_otcs.py

Script de subida de ficheros (.txt y .pdf) a un nodo
de OpenText Content Management (xECM / Extended ECM) mediante la API REST,
asignando la categoría "Raw material" con los atributos "Name" y "Ticker".

Uso:
    python upload_to_otcs.py --input ./ficheros --node 123456
    python upload_to_otcs.py -i ./ficheros -n 123456
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import requests
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Configuración
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OTCSConfig:
    """Configuración de conexión a OpenText Content Management, cargada desde .env."""

    base_url: str
    username: str
    password: str
    category_id: str
    attr_name_id: str
    attr_ticker_id: str
    domain: str = ""
    classification_ids: tuple[str, ...] = ()

    REQUIRED_ENV_VARS = {
        "OTCS_URL": "base_url",
        "OTCS_USERNAME": "username",
        "OTCS_PASSWORD": "password",
        "OTCS_CATEGORY_ID": "category_id",
        "OTCS_ATTR_NAME_ID": "attr_name_id",
        "OTCS_ATTR_TICKER_ID": "attr_ticker_id",
    }

    @classmethod
    def from_env(cls) -> "OTCSConfig":
        load_dotenv()

        values: dict[str, str] = {}
        missing: list[str] = []
        for env_var, field_name in cls.REQUIRED_ENV_VARS.items():
            value = os.getenv(env_var)
            if not value:
                missing.append(env_var)
            else:
                values[field_name] = value

        if missing:
            logger.error(
                "Faltan variables de entorno en el fichero .env: %s", ", ".join(missing)
            )
            sys.exit(1)

        values["domain"] = os.getenv("OTCS_DOMAIN", "")

        raw_classification_ids = os.getenv("OTCS_CLASSIFICATION_IDS", "")
        values["classification_ids"] = tuple(
            part.strip() for part in raw_classification_ids.split(",") if part.strip()
        )

        config = cls(**values)
        config._validate_attribute_ids()
        return config

    def _validate_attribute_ids(self) -> None:
        """Avisa si un atributo usa el sufijo '_1', reservado a la propia categoría."""
        for label, attr_id in (
            ("OTCS_ATTR_NAME_ID", self.attr_name_id),
            ("OTCS_ATTR_TICKER_ID", self.attr_ticker_id),
        ):
            if attr_id == f"{self.category_id}_1":
                logger.warning(
                    "%s='%s' parece inválido: el sufijo '_1' está reservado a la "
                    "categoría y no identifica un atributo. Obtén el ID real con "
                    "GET /api/v2/nodes/{id}?fields=categories sobre un documento "
                    "ya categorizado (normalmente empieza en '_2').",
                    label,
                    attr_id,
                )

    @property
    def auth_endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/api/v1/auth"

    @property
    def nodes_endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/api/v1/nodes"


# --------------------------------------------------------------------------- #
# Resolución de metadatos a partir del nombre de fichero
# --------------------------------------------------------------------------- #
class TickerResolver:
    """Extrae el ticker del nombre de fichero y lo resuelve a un nombre descriptivo."""

    DEFAULT_NAME_MAP: dict[str, str] = {
        "XAUUSD": "Gold - XAUUSD",
        "XPTUSD": "Platinum - XPTUSD",
        "XAGUSD": "Silver - XAGUSD",
    }

    def __init__(self, name_map: dict[str, str] | None = None) -> None:
        self._name_map = name_map or dict(self.DEFAULT_NAME_MAP)

    def extract_ticker(self, filename: str) -> str:
        """Ticker = parte anterior al primer '-' del nombre de fichero (sin extensión)."""
        stem = Path(filename).stem
        return stem.split("-", 1)[0].upper()

    def extract_date(self, filename: str) -> str:
        """Date = parte posterior al primer '-' del nombre de fichero (sin extensión)."""
        stem = Path(filename).stem
        parts = stem.split("-", 1)
        return parts[1].upper() if len(parts) > 1 else ""

    def resolve_name(self, ticker: str, date: str) -> str:
        """Devuelve '<Nombre descriptivo> - <fecha>', o '<ticker> - <fecha>' si el ticker es desconocido."""
        try:
            base_name = self._name_map[ticker]
        except KeyError:
            logger.warning(
                "Ticker '%s' no está en el diccionario de nombres conocidos; "
                "se usará el propio ticker como Name.",
                ticker,
            )
            base_name = ticker

        return f"{base_name} - {date}" if date else base_name


# --------------------------------------------------------------------------- #
# Representación de un documento a subir
# --------------------------------------------------------------------------- #
@dataclass
class DocumentMetadata:
    """Metadatos de categoría resueltos para un fichero concreto."""

    file_path: Path
    ticker: str
    name: str


# --------------------------------------------------------------------------- #
# Autenticación contra OTCS (POST /api/v1/auth)
# --------------------------------------------------------------------------- #
class OTCSAuthenticator:
    """Obtiene un ticket de autenticación llamando a POST /api/v1/auth."""

    REQUEST_TIMEOUT = 30  # segundos

    def __init__(self, config: OTCSConfig, session: requests.Session) -> None:
        self._config = config
        self._session = session

    def authenticate(self) -> str:
        """Autentica al usuario y devuelve el ticket devuelto por OTCS."""
        credentials = {
            "username": self._config.username,
            "password": self._config.password,
            "domain": self._config.domain,
        }

        logger.info("Autenticando usuario '%s' contra %s...", self._config.username, self._config.auth_endpoint)

        try:
            response = self._session.post(
                self._config.auth_endpoint,
                json=credentials,
                timeout=self.REQUEST_TIMEOUT,
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            logger.error("Error al autenticar contra OTCS: %s", exc)
            if exc.response is not None:
                logger.error("Respuesta del servidor: %s", exc.response.text)
            raise

        ticket = response.json().get("ticket")
        if not ticket:
            raise RuntimeError(
                "La respuesta de /api/v1/auth no contiene un 'ticket' válido."
            )

        logger.info("Autenticación correcta.")
        return ticket


# --------------------------------------------------------------------------- #
# Cliente de la API REST de OTCS
# --------------------------------------------------------------------------- #
class OTCSClient:
    """Encapsula las llamadas HTTP a la API REST de OpenText Content Management."""

    DOCUMENT_TYPE = 144  # Tipo de nodo "Documento" en OTCS
    REQUEST_TIMEOUT = 60  # segundos

    def __init__(self, config: OTCSConfig, session: requests.Session | None = None) -> None:
        self._config = config
        self._session = session or requests.Session()
        self._ticket: str | None = None

    def __enter__(self) -> "OTCSClient":
        authenticator = OTCSAuthenticator(self._config, self._session)
        self._ticket = authenticator.authenticate()
        return self

    def __exit__(self, *exc_info) -> None:
        self._session.close()

    @property
    def _auth_headers(self) -> dict[str, str]:
        if not self._ticket:
            raise RuntimeError(
                "OTCSClient no está autenticado; úsalo dentro de un bloque 'with'."
            )
        return {"OTCSTicket": self._ticket}

    def _build_node_body(self, parent_id: str, metadata: DocumentMetadata) -> dict:
        roles: dict = {
            "categories": {
                self._config.category_id: {
                    self._config.attr_name_id: metadata.name,
                    self._config.attr_ticker_id: metadata.ticker,
                }
            }
        }

        if self._config.classification_ids:
            roles["classifications"] = {
                "create_id": [int(cid) for cid in self._config.classification_ids]
            }

        return {
            "type": self.DOCUMENT_TYPE,
            "parent_id": parent_id,
            "name": metadata.file_path.name,
            "description": "",
            "advanced_versioning": False,
            "roles": roles,
        }

    def upload_document(self, parent_id: str, metadata: DocumentMetadata) -> bool:
        """Sube un único documento asignando el MIME Type correspondiente."""
        body = self._build_node_body(parent_id, metadata)

        # Determinar de forma dinámica el MIME Type según el fichero
        mime_type, _ = mimetypes.guess_type(metadata.file_path)
        if not mime_type:
            # Valor fallback por si acaso no reconoce la extensión
            mime_type = "application/octet-stream"

        logger.info(
            "Subiendo '%s' [MIME: %s] (Ticker=%s, Name=%s)...",
            metadata.file_path.name,
            mime_type,
            metadata.ticker,
            metadata.name,
        )

        try:
            with metadata.file_path.open("rb") as file_handle:
                multipart_fields = {
                    "body": (None, json.dumps(body), "application/json"),
                    "file": (metadata.file_path.name, file_handle, mime_type),
                }
                response = self._session.post(
                    self._config.nodes_endpoint,
                    headers=self._auth_headers,
                    files=multipart_fields,
                    timeout=self.REQUEST_TIMEOUT,
                )
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            logger.error("Error al subir '%s': %s", metadata.file_path.name, exc)
            if exc.response is not None:
                logger.error("Respuesta del servidor: %s", exc.response.text)
            return False

        logger.info("'%s' subido correctamente.", metadata.file_path.name)
        return True


# --------------------------------------------------------------------------- #
# Orquestador del proceso de subida
# --------------------------------------------------------------------------- #
@dataclass
class UploadReport:
    """Resumen del resultado de una tanda de subidas."""

    succeeded: int = 0
    failed: int = 0

    def record(self, success: bool) -> None:
        if success:
            self.succeeded += 1
        else:
            self.failed += 1

    @property
    def has_failures(self) -> bool:
        return self.failed > 0


class BulkUploader:
    """Coordina la búsqueda de ficheros, la resolución de metadatos y la subida a OTCS."""

    def __init__(
        self,
        client: OTCSClient,
        resolver: TickerResolver,
        node_id: str,
    ) -> None:
        self._client = client
        self._resolver = resolver
        self._node_id = node_id

    def _find_files(self, input_dir: Path) -> list[Path]:
        if not input_dir.is_dir():
            logger.error(
                "La carpeta de entrada '%s' no existe o no es un directorio.", input_dir
            )
            sys.exit(1)

        # Buscar ficheros con extensión .txt y .pdf
        files = sorted(
            [
                p for p in input_dir.iterdir()
                if p.is_file() and p.suffix.lower() in (".txt", ".pdf")
            ]
        )

        if not files:
            logger.warning("No se han encontrado ficheros .txt o .pdf en '%s'.", input_dir)

        return files

    def _build_metadata(self, file_path: Path) -> DocumentMetadata:
        ticker = self._resolver.extract_ticker(file_path.name)
        str_date = self._resolver.extract_date(file_path.name)
        name = self._resolver.resolve_name(ticker, str_date)
        return DocumentMetadata(file_path=file_path, ticker=ticker, name=name)

    def run(self, input_dir: Path) -> UploadReport:
        files = self._find_files(input_dir)
        report = UploadReport()

        if not files:
            return report

        logger.info("Se subirán %d fichero(s) al nodo %s.", len(files), self._node_id)

        for file_path in files:
            metadata = self._build_metadata(file_path)
            success = self._client.upload_document(self._node_id, metadata)
            report.record(success)

        logger.info(
            "Proceso finalizado. Subidos correctamente: %d. Con errores: %d.",
            report.succeeded,
            report.failed,
        )
        return report


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
class CLI:
    """Punto de entrada de línea de comandos de la aplicación."""

    @staticmethod
    def parse_args() -> argparse.Namespace:
        parser = argparse.ArgumentParser(
            description="Sube ficheros .txt y .pdf a un nodo de OpenText Content Management (xECM)."
        )
        parser.add_argument(
            "-i",
            "--input",
            required=True,
            type=Path,
            help="Carpeta local desde la que se leen los ficheros .txt o .pdf",
        )
        parser.add_argument(
            "-n",
            "--node",
            required=True,
            help="ID del nodo raíz de xECM donde se subirán los archivos",
        )
        return parser.parse_args()

    @classmethod
    def run(cls) -> None:
        args = cls.parse_args()
        config = OTCSConfig.from_env()
        resolver = TickerResolver()

        with OTCSClient(config) as client:
            uploader = BulkUploader(client=client, resolver=resolver, node_id=args.node)
            report = uploader.run(args.input)

        sys.exit(1 if report.has_failures else 0)


def main() -> None:
    CLI.run()


if __name__ == "__main__":
    main()