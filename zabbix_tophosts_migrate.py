#!/usr/bin/env python3
"""Safely migrate Zabbix 7.4 Top hosts widgets to Fixed Top hosts via JSON-RPC API."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import decimal
import getpass
import hashlib
import json
import math
import os
import re
import ssl
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable


SCRIPT_VERSION = "1.0.0"
BACKUP_FORMAT = "zabbix-fixedtophosts-migration-backup-v1"
SOURCE_WIDGET_TYPE = "tophosts"
TARGET_WIDGET_TYPE = "fixedtophosts"
FIELD_TYPE_INT32 = 0
FIELD_TYPE_STR = 1
DATA_HOST_NAME = 2
DISPLAY_SPARKLINE = 6
SECONDARY_ORDER = {"asc": 0, "desc": 1}


class MigrationError(RuntimeError):
	"""Expected migration failure with a user-facing message."""


class ApiError(MigrationError):
	def __init__(self, method: str, error: dict[str, Any]):
		self.method = method
		self.code = error.get("code")
		self.message = str(error.get("message", "Unknown API error"))
		self.data = str(error.get("data", ""))
		suffix = f": {self.data}" if self.data else ""
		super().__init__(f"Zabbix API {method}: {self.message}{suffix}")


class ZabbixApi:
	def __init__(self, url: str, *, timeout: float = 30, insecure: bool = False):
		self.url = normalize_api_url(url)
		self.timeout = timeout
		self.token: str | None = None
		self._request_id = 0
		self._ssl_context = ssl._create_unverified_context() if insecure else ssl.create_default_context()

	def call(self, method: str, params: Any, *, authenticated: bool = True) -> Any:
		self._request_id += 1
		payload = json.dumps(
			{"jsonrpc": "2.0", "method": method, "params": params, "id": self._request_id},
			ensure_ascii=False,
			separators=(",", ":"),
		).encode("utf-8")
		headers = {
			"Content-Type": "application/json-rpc",
			"Accept": "application/json",
			"User-Agent": f"fixedtophosts-migrator/{SCRIPT_VERSION}",
		}

		if authenticated:
			if not self.token:
				raise MigrationError("Для этого запроса отсутствует токен авторизации Zabbix.")
			headers["Authorization"] = f"Bearer {self.token}"

		request = urllib.request.Request(self.url, data=payload, headers=headers, method="POST")

		try:
			with urllib.request.urlopen(request, timeout=self.timeout, context=self._ssl_context) as response:
				body = response.read()
		except urllib.error.HTTPError as error:
			body = error.read().decode("utf-8", "replace")
			raise MigrationError(f"HTTP {error.code} от Zabbix API: {body[:500]}") from error
		except urllib.error.URLError as error:
			raise MigrationError(f"Не удалось подключиться к {self.url}: {error.reason}") from error

		try:
			result = json.loads(body.decode("utf-8"))
		except (UnicodeDecodeError, json.JSONDecodeError) as error:
			raise MigrationError("Zabbix API вернул некорректный JSON.") from error

		if not isinstance(result, dict):
			raise MigrationError("Zabbix API вернул неожиданный формат ответа.")
		if "error" in result:
			raise ApiError(method, result["error"])
		if "result" not in result:
			raise MigrationError(f"В ответе Zabbix API на {method} отсутствует result.")

		return result["result"]


def normalize_api_url(url: str) -> str:
	url = url.strip().rstrip("/")
	if not url:
		raise MigrationError("Адрес Zabbix API не задан.")
	if not url.endswith("/api_jsonrpc.php"):
		url += "/api_jsonrpc.php"
	return url


def dashboard_digest(dashboard: dict[str, Any]) -> str:
	raw = json.dumps(dashboard, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
	return hashlib.sha256(raw).hexdigest()


def field_map(widget: dict[str, Any]) -> dict[str, dict[str, Any]]:
	return {str(field.get("name", "")): field for field in widget.get("fields", [])}


def get_field_value(widget: dict[str, Any], name: str, default: Any = None) -> Any:
	field = field_map(widget).get(name)
	return default if field is None else field.get("value", default)


def set_field(widget: dict[str, Any], name: str, field_type: int, value: Any) -> None:
	fields = widget.setdefault("fields", [])
	for field in fields:
		if field.get("name") == name:
			field["type"] = field_type
			field["value"] = value
			return
	fields.append({"type": field_type, "name": name, "value": value})


def column_indexes(widget: dict[str, Any]) -> list[int]:
	indexes: set[int] = set()
	for name in field_map(widget):
		match = re.fullmatch(r"columns\.(\d+)\.data", name)
		if match:
			indexes.add(int(match.group(1)))
	return sorted(indexes)


def host_name_column(widget: dict[str, Any]) -> int | None:
	for index in column_indexes(widget):
		try:
			data_type = int(get_field_value(widget, f"columns.{index}.data"))
		except (TypeError, ValueError):
			continue
		if data_type == DATA_HOST_NAME:
			return index
	return None


def sparkline_columns(widget: dict[str, Any]) -> list[int]:
	result = []
	for index in column_indexes(widget):
		try:
			display = int(get_field_value(widget, f"columns.{index}.display", 1))
		except (TypeError, ValueError):
			continue
		if display == DISPLAY_SPARKLINE:
			result.append(index)
	return result


def column_caption(widget: dict[str, Any], index: int) -> str:
	name = str(get_field_value(widget, f"columns.{index}.name", "")).strip()
	if name:
		return name

	try:
		data_type = int(get_field_value(widget, f"columns.{index}.data"))
	except (TypeError, ValueError):
		data_type = None

	if data_type == DATA_HOST_NAME:
		return "Host name"

	for suffix in ("item", "text"):
		value = str(get_field_value(widget, f"columns.{index}.{suffix}", "")).strip()
		if value:
			return value
	return f"Колонка {index + 1}"


def iter_widgets(dashboard: dict[str, Any]) -> Iterable[tuple[int, dict[str, Any], dict[str, Any]]]:
	for page_number, page in enumerate(dashboard.get("pages", []), start=1):
		for widget in page.get("widgets", []):
			yield page_number, page, widget


def widget_index(dashboard: dict[str, Any]) -> dict[str, tuple[int, dict[str, Any], dict[str, Any]]]:
	return {str(widget["widgetid"]): (page_number, page, widget)
		for page_number, page, widget in iter_widgets(dashboard)}


def select_target_widgetids(dashboard: dict[str, Any], requested: list[str] | None) -> list[str]:
	widgets = widget_index(dashboard)
	if requested:
		requested_ids = list(dict.fromkeys(str(widgetid) for widgetid in requested))
		missing = [widgetid for widgetid in requested_ids if widgetid not in widgets]
		wrong_type = [widgetid for widgetid in requested_ids
			if widgetid in widgets and widgets[widgetid][2].get("type") != SOURCE_WIDGET_TYPE]
		if missing:
			raise MigrationError("На панели не найдены widgetid: "+", ".join(missing))
		if wrong_type:
			raise MigrationError("Указанные виджеты не имеют тип tophosts: "+", ".join(wrong_type))
		return requested_ids

	return [str(widget["widgetid"]) for _, _, widget in iter_widgets(dashboard)
		if widget.get("type") == SOURCE_WIDGET_TYPE]


def prepare_migration(dashboard: dict[str, Any], target_widgetids: list[str], *,
		secondary_hostname: bool = False, secondary_order: str = "asc",
		sparkline_bounds: tuple[str, str] | None = None) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
	expected = copy.deepcopy(dashboard)
	targets = set(target_widgetids)
	report: dict[str, dict[str, Any]] = {}

	for _, _, widget in iter_widgets(expected):
		widgetid = str(widget["widgetid"])
		if widgetid not in targets:
			continue

		widget["type"] = TARGET_WIDGET_TYPE
		changes: dict[str, Any] = {"fields_changed": False, "secondary": None, "sparklines": []}

		if secondary_hostname:
			host_column = host_name_column(widget)
			try:
				primary_column = int(get_field_value(widget, "column", 0))
			except (TypeError, ValueError):
				primary_column = 0

			if host_column is None:
				changes["secondary"] = "no-hostname-column"
			elif host_column == primary_column:
				changes["secondary"] = "same-as-primary"
			else:
				set_field(widget, "column_secondary", FIELD_TYPE_INT32, host_column)
				set_field(widget, "order_secondary", FIELD_TYPE_INT32, SECONDARY_ORDER[secondary_order])
				changes["fields_changed"] = True
				changes["secondary"] = {
					"column": host_column,
					"caption": column_caption(widget, host_column),
					"order": secondary_order,
				}

		if sparkline_bounds is not None:
			minimum, maximum = sparkline_bounds
			for index in sparkline_columns(widget):
				set_field(widget, f"columns.{index}.min", FIELD_TYPE_STR, minimum)
				set_field(widget, f"columns.{index}.max", FIELD_TYPE_STR, maximum)
				changes["sparklines"].append({
					"column": index,
					"caption": column_caption(widget, index),
					"min": minimum,
					"max": maximum,
				})
			if changes["sparklines"]:
				changes["fields_changed"] = True

		report[widgetid] = changes

	return expected, report


def prepare_restore(current: dict[str, Any], backup_dashboard: dict[str, Any],
		target_widgetids: list[str]) -> dict[str, Any]:
	expected = copy.deepcopy(current)
	current_widgets = widget_index(expected)
	backup_widgets = widget_index(backup_dashboard)

	missing_current = [widgetid for widgetid in target_widgetids if widgetid not in current_widgets]
	missing_backup = [widgetid for widgetid in target_widgetids if widgetid not in backup_widgets]
	if missing_current:
		raise MigrationError("Нельзя восстановить отсутствующие сейчас widgetid: "+", ".join(missing_current))
	if missing_backup:
		raise MigrationError("В резервной копии отсутствуют widgetid: "+", ".join(missing_backup))

	for widgetid in target_widgetids:
		current_widget = current_widgets[widgetid][2]
		backup_widget = backup_widgets[widgetid][2]
		current_widget["type"] = backup_widget["type"]
		current_widget["fields"] = copy.deepcopy(backup_widget.get("fields", []))

	return expected


def build_update_payload(current: dict[str, Any], expected: dict[str, Any], target_widgetids: list[str],
		field_widgetids: set[str]) -> dict[str, Any]:
	expected_widgets = widget_index(expected)
	targets = set(target_widgetids)
	pages = []

	for page in current.get("pages", []):
		page_update: dict[str, Any] = {"dashboard_pageid": page["dashboard_pageid"]}
		widgets = []
		for current_widget in page.get("widgets", []):
			widgetid = str(current_widget["widgetid"])
			widget_update: dict[str, Any] = {"widgetid": current_widget["widgetid"]}
			if widgetid in targets:
				expected_widget = expected_widgets[widgetid][2]
				widget_update["type"] = expected_widget["type"]
				if widgetid in field_widgetids:
					widget_update["fields"] = copy.deepcopy(expected_widget.get("fields", []))
			widgets.append(widget_update)
		page_update["widgets"] = widgets
		pages.append(page_update)

	return {"dashboardid": current["dashboardid"], "pages": pages}


def canonical_widget(widget: dict[str, Any]) -> dict[str, Any]:
	return {
		"widgetid": str(widget.get("widgetid", "")),
		"type": str(widget.get("type", "")),
		"name": str(widget.get("name", "")),
		"x": str(widget.get("x", "")),
		"y": str(widget.get("y", "")),
		"width": str(widget.get("width", "")),
		"height": str(widget.get("height", "")),
		"view_mode": str(widget.get("view_mode", "")),
		"fields": sorted(
			(str(field.get("type", "")), str(field.get("name", "")), str(field.get("value", "")))
			for field in widget.get("fields", [])
		),
	}


def verify_dashboard(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
	errors: list[str] = []
	if str(expected.get("dashboardid")) != str(actual.get("dashboardid")):
		errors.append("Изменился dashboardid.")
	if str(expected.get("name", "")) != str(actual.get("name", "")):
		errors.append("Изменилось имя панели.")

	expected_pages = expected.get("pages", [])
	actual_pages = actual.get("pages", [])
	if [str(page.get("dashboard_pageid")) for page in expected_pages] != [
		str(page.get("dashboard_pageid")) for page in actual_pages
	]:
		errors.append("Изменился состав или порядок страниц.")
		return errors

	for expected_page, actual_page in zip(expected_pages, actual_pages):
		pageid = str(expected_page.get("dashboard_pageid"))
		for key in ("name", "display_period"):
			if str(expected_page.get(key, "")) != str(actual_page.get(key, "")):
				errors.append(f"Страница {pageid}: изменилось поле {key}.")

		expected_widgets = {str(widget["widgetid"]): canonical_widget(widget)
			for widget in expected_page.get("widgets", [])}
		actual_widgets = {str(widget["widgetid"]): canonical_widget(widget)
			for widget in actual_page.get("widgets", [])}
		if expected_widgets.keys() != actual_widgets.keys():
			errors.append(f"Страница {pageid}: изменился состав виджетов.")
			continue
		for widgetid in expected_widgets:
			if expected_widgets[widgetid] != actual_widgets[widgetid]:
				errors.append(f"Страница {pageid}: виджет {widgetid} отличается от ожидаемого состояния.")

	return errors


def fetch_dashboard(api: ZabbixApi, dashboardid: str) -> dict[str, Any]:
	result = api.call("dashboard.get", {
		"output": ["dashboardid", "name"],
		"dashboardids": [dashboardid],
		"editable": True,
		"selectPages": ["dashboard_pageid", "name", "display_period", "widgets"],
	})
	if not result:
		raise MigrationError(f"Редактируемая панель dashboardid={dashboardid} не найдена.")
	if len(result) != 1:
		raise MigrationError(f"Zabbix вернул несколько панелей для dashboardid={dashboardid}.")
	return result[0]


def check_module(api: ZabbixApi, *, applying: bool) -> None:
	try:
		modules = api.call("module.get", {
			"output": ["id", "relative_path", "status"],
			"filter": {"id": [TARGET_WIDGET_TYPE]},
		})
	except ApiError as error:
		print(f"ПРЕДУПРЕЖДЕНИЕ: статус модуля проверить нельзя ({error.message}).")
		return

	enabled = any(module.get("id") == TARGET_WIDGET_TYPE and int(module.get("status", 0)) == 1
		for module in modules)
	if not enabled:
		message = "Модуль fixedtophosts не найден или выключен в Zabbix."
		if applying:
			raise MigrationError(message+" Миграция остановлена.")
		print("ПРЕДУПРЕЖДЕНИЕ: "+message)


def validate_bounds(minimum: str | None, maximum: str | None) -> tuple[str, str] | None:
	if minimum is None and maximum is None:
		return None
	if minimum is None or maximum is None:
		raise MigrationError("Параметры --sparkline-min и --sparkline-max нужно указывать вместе.")
	try:
		min_value = decimal.Decimal(minimum)
		max_value = decimal.Decimal(maximum)
	except decimal.InvalidOperation as error:
		raise MigrationError("Границы спарклайна должны быть обычными числами, например 0 и 100.") from error
	if not min_value.is_finite() or not max_value.is_finite() or min_value >= max_value:
		raise MigrationError("Граница Min должна быть конечным числом и быть меньше Max.")
	return minimum, maximum


def backup_document(dashboard: dict[str, Any], *, zabbix_version: str, api_url: str,
		target_widgetids: list[str], operation: str, options: dict[str, Any]) -> dict[str, Any]:
	return {
		"format": BACKUP_FORMAT,
		"script_version": SCRIPT_VERSION,
		"created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
		"zabbix_version": zabbix_version,
		"api_url": api_url,
		"operation": operation,
		"source_widget_type": SOURCE_WIDGET_TYPE,
		"target_widget_type": TARGET_WIDGET_TYPE,
		"target_widgetids": target_widgetids,
		"options": options,
		"dashboard_sha256": dashboard_digest(dashboard),
		"dashboard": dashboard,
	}


def write_backup(document: dict[str, Any], directory: Path) -> Path:
	directory.mkdir(mode=0o700, parents=True, exist_ok=True)
	os.chmod(directory, 0o700)
	dashboardid = str(document["dashboard"]["dashboardid"])
	timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
	base_name = f"dashboard-{dashboardid}-{document['operation']}-{timestamp}.json"
	path = directory / base_name
	counter = 1
	while path.exists():
		path = directory / f"{Path(base_name).stem}-{counter}.json"
		counter += 1

	raw = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")+b"\n"
	with tempfile.NamedTemporaryFile(prefix=".fixedtophosts-", dir=directory, delete=False) as temporary:
		temporary.write(raw)
		temporary.flush()
		os.fsync(temporary.fileno())
		temporary_path = Path(temporary.name)
	os.chmod(temporary_path, 0o600)
	os.replace(temporary_path, path)
	return path


def load_backup(path: Path) -> dict[str, Any]:
	try:
		document = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as error:
		raise MigrationError(f"Не удалось прочитать резервную копию {path}: {error}") from error
	if not isinstance(document, dict) or document.get("format") != BACKUP_FORMAT:
		raise MigrationError("Файл не является резервной копией этого скрипта.")
	dashboard = document.get("dashboard")
	if not isinstance(dashboard, dict) or "dashboardid" not in dashboard or "pages" not in dashboard:
		raise MigrationError("В резервной копии отсутствует полное описание панели.")
	if document.get("dashboard_sha256") != dashboard_digest(dashboard):
		raise MigrationError("Контрольная сумма резервной копии не совпадает: файл был изменён или повреждён.")
	widgetids = document.get("target_widgetids")
	if not isinstance(widgetids, list) or not widgetids:
		raise MigrationError("В резервной копии отсутствует список мигрированных виджетов.")
	document["target_widgetids"] = [str(widgetid) for widgetid in widgetids]
	return document


def print_targets(dashboard: dict[str, Any], target_widgetids: list[str], report: dict[str, dict[str, Any]]) -> None:
	print(f"Панель: {dashboard.get('name', '')} (dashboardid={dashboard.get('dashboardid')})")
	print(f"Найдено для миграции: {len(target_widgetids)}")
	index = widget_index(dashboard)
	for widgetid in target_widgetids:
		page_number, page, widget = index[widgetid]
		page_name = str(page.get("name", "")).strip() or f"страница {page_number}"
		widget_name = str(widget.get("name", "")).strip() or "Top hosts"
		columns = ", ".join(column_caption(widget, column) for column in column_indexes(widget))
		print(f"  widgetid={widgetid}: {page_name} / {widget_name}; колонки: {columns or 'не определены'}")
		changes = report[widgetid]
		secondary = changes.get("secondary")
		if isinstance(secondary, dict):
			direction = "A→Z / 0→9" if secondary["order"] == "asc" else "Z→A / 9→0"
			print(f"    второй уровень: {secondary['caption']} ({direction})")
		elif secondary == "no-hostname-column":
			print("    второй уровень: пропущен — нет колонки Host name")
		elif secondary == "same-as-primary":
			print("    второй уровень: пропущен — Host name уже является первой сортировкой")
		for sparkline in changes.get("sparklines", []):
			print(f"    спарклайн {sparkline['caption']}: {sparkline['min']}…{sparkline['max']}")


def list_dashboards(api: ZabbixApi) -> None:
	dashboards = api.call("dashboard.get", {
		"output": ["dashboardid", "name"],
		"editable": True,
		"selectPages": ["dashboard_pageid", "widgets"],
	})
	rows = []
	for dashboard in dashboards:
		count = sum(1 for _, _, widget in iter_widgets(dashboard) if widget.get("type") == SOURCE_WIDGET_TYPE)
		rows.append((str(dashboard["name"]), str(dashboard["dashboardid"]), count))
	for name, dashboardid, count in sorted(rows, key=lambda row: row[0].casefold()):
		print(f"dashboardid={dashboardid:>6}  Top hosts={count:>3}  {name}")


def confirm(action: str, dashboard: dict[str, Any], count: int) -> None:
	word = "MIGRATE" if action == "migrate" else "RESTORE"
	print()
	print("ВНИМАНИЕ: сейчас будет изменена рабочая панель через Zabbix API.")
	print("Службы Zabbix не перезапускаются, но открытая панель изменится сразу после обновления страницы.")
	answer = input(f"Для панели «{dashboard.get('name', '')}» и {count} виджетов введите {word}: ").strip()
	if answer != word:
		raise MigrationError("Подтверждение не получено; изменения не выполнялись.")


def authenticate(api: ZabbixApi, username: str | None) -> bool:
	token = os.environ.get("ZABBIX_API_TOKEN", "").strip()
	if token:
		api.token = token
		print("Авторизация: токен из переменной ZABBIX_API_TOKEN.")
		return False

	if username is None:
		username = input("Имя пользователя Zabbix: ").strip()
	if not username:
		raise MigrationError("Имя пользователя Zabbix не задано.")
	password = getpass.getpass("Пароль Zabbix (не отображается): ")
	api.token = api.call("user.login", {"username": username, "password": password}, authenticated=False)
	print(f"Авторизация: пользователь {username}.")
	return True


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		description="Безопасная миграция Top hosts → Top hosts (fixed scale) в Zabbix 7.4.",
		epilog=(
			"По умолчанию выполняется только предварительный просмотр. Для токена задайте его без попадания "
			"в историю команд: export ZABBIX_API_TOKEN='…'. Без токена скрипт безопасно запросит пароль."
		),
		formatter_class=argparse.RawDescriptionHelpFormatter,
	)
	mode = parser.add_mutually_exclusive_group(required=True)
	mode.add_argument("--list", action="store_true", help="показать редактируемые панели и число Top hosts")
	mode.add_argument("--dashboard-id", help="dashboardid панели для просмотра или миграции")
	mode.add_argument("--restore", type=Path, help="подготовить или применить восстановление из JSON-копии")
	parser.add_argument("--api-url", default="http://127.0.0.1/zabbix/api_jsonrpc.php",
		help="URL Zabbix или api_jsonrpc.php (по умолчанию: %(default)s)")
	parser.add_argument("--username", help="пользователь Zabbix; пароль будет запрошен скрыто")
	parser.add_argument("--widget-id", action="append", help="мигрировать только указанный widgetid; можно повторять")
	parser.add_argument("--secondary-hostname", action="store_true",
		help="сразу настроить Host name как второй уровень сортировки")
	parser.add_argument("--secondary-order", choices=("asc", "desc"), default="asc",
		help="направление второго уровня (по умолчанию: asc)")
	parser.add_argument("--sparkline-min", help="необязательно: Min для всех спарклайнов выбранных виджетов")
	parser.add_argument("--sparkline-max", help="необязательно: Max для всех спарклайнов выбранных виджетов")
	parser.add_argument("--apply", action="store_true", help="выполнить показанные изменения")
	parser.add_argument("--yes", action="store_true", help="не запрашивать слово-подтверждение вместе с --apply")
	parser.add_argument("--backup-dir", type=Path, default=Path.cwd()/"zabbix-dashboard-backups",
		help="каталог JSON-копий (по умолчанию: ./zabbix-dashboard-backups)")
	parser.add_argument("--timeout", type=float, default=30, help="тайм-аут API в секундах")
	parser.add_argument("--insecure", action="store_true",
		help="разрешить недоверенный HTTPS-сертификат (только при явной необходимости)")
	return parser


def validate_arguments(args: argparse.Namespace) -> tuple[str, str] | None:
	if args.yes and not args.apply:
		raise MigrationError("Параметр --yes допустим только вместе с --apply.")
	if args.timeout <= 0 or not math.isfinite(args.timeout):
		raise MigrationError("Параметр --timeout должен быть положительным числом.")
	if args.restore is not None and (args.widget_id or args.secondary_hostname
			or args.sparkline_min is not None or args.sparkline_max is not None):
		raise MigrationError("При --restore нельзя задавать параметры миграции виджетов.")
	if args.list and (args.widget_id or args.secondary_hostname or args.apply
			or args.sparkline_min is not None or args.sparkline_max is not None):
		raise MigrationError("С --list нельзя задавать параметры миграции или --apply.")
	return validate_bounds(args.sparkline_min, args.sparkline_max)


def run_migration(api: ZabbixApi, args: argparse.Namespace, zabbix_version: str,
		sparkline_bounds: tuple[str, str] | None) -> None:
	check_module(api, applying=args.apply)
	dashboard = fetch_dashboard(api, str(args.dashboard_id))
	target_widgetids = select_target_widgetids(dashboard, args.widget_id)
	if not target_widgetids:
		print("На панели нет виджетов типа Top hosts; изменять нечего.")
		return

	expected, report = prepare_migration(
		dashboard,
		target_widgetids,
		secondary_hostname=args.secondary_hostname,
		secondary_order=args.secondary_order,
		sparkline_bounds=sparkline_bounds,
	)
	print_targets(dashboard, target_widgetids, report)
	print("Режим: ПРИМЕНЕНИЕ" if args.apply else "Режим: только предварительный просмотр; изменений нет.")
	if not args.apply:
		return
	if not args.yes:
		confirm("migrate", dashboard, len(target_widgetids))

	# The confirmation dialog can remain open for a while. Refuse to overwrite a
	# dashboard that somebody edited after this run produced its preview.
	latest = fetch_dashboard(api, str(dashboard["dashboardid"]))
	concurrent_errors = verify_dashboard(dashboard, latest)
	if concurrent_errors:
		print("Панель изменилась после предварительного чтения:", file=sys.stderr)
		for error in concurrent_errors:
			print("  - "+error, file=sys.stderr)
		raise MigrationError("Миграция остановлена до записи. Запустите предварительный просмотр повторно.")
	dashboard = latest
	expected, report = prepare_migration(
		dashboard,
		target_widgetids,
		secondary_hostname=args.secondary_hostname,
		secondary_order=args.secondary_order,
		sparkline_bounds=sparkline_bounds,
	)

	options = {
		"secondary_hostname": args.secondary_hostname,
		"secondary_order": args.secondary_order,
		"sparkline_bounds": list(sparkline_bounds) if sparkline_bounds is not None else None,
	}
	backup = backup_document(
		dashboard,
		zabbix_version=zabbix_version,
		api_url=api.url,
		target_widgetids=target_widgetids,
		operation="before-migration",
		options=options,
	)
	backup_path = write_backup(backup, args.backup_dir)
	print(f"Резервная копия создана: {backup_path}")

	field_widgetids = {widgetid for widgetid, changes in report.items() if changes["fields_changed"]}
	update = build_update_payload(dashboard, expected, target_widgetids, field_widgetids)
	api.call("dashboard.update", update)
	actual = fetch_dashboard(api, str(dashboard["dashboardid"]))
	errors = verify_dashboard(expected, actual)
	if errors:
		print("ПРОВЕРКА ПОСЛЕ API НЕ ПРОШЛА:", file=sys.stderr)
		for error in errors:
			print("  - "+error, file=sys.stderr)
		print(f"Для восстановления используйте: --restore {backup_path} --apply", file=sys.stderr)
		raise MigrationError("Результат отличается от ожидаемого; автоматический откат не выполнялся.")
	print(f"Готово: {len(target_widgetids)} виджетов переведено в {TARGET_WIDGET_TYPE}; остальные не изменились.")


def run_restore(api: ZabbixApi, args: argparse.Namespace, zabbix_version: str) -> None:
	backup = load_backup(args.restore)
	backup_dashboard = backup["dashboard"]
	dashboardid = str(backup_dashboard["dashboardid"])
	current = fetch_dashboard(api, dashboardid)
	target_widgetids = backup["target_widgetids"]
	expected = prepare_restore(current, backup_dashboard, target_widgetids)

	print(f"Панель: {current.get('name', '')} (dashboardid={dashboardid})")
	print(f"Будут восстановлены тип и поля {len(target_widgetids)} виджетов: {', '.join(target_widgetids)}")
	print("Режим: ВОССТАНОВЛЕНИЕ" if args.apply else "Режим: только предварительный просмотр; изменений нет.")
	if not args.apply:
		return
	if not args.yes:
		confirm("restore", current, len(target_widgetids))

	latest = fetch_dashboard(api, dashboardid)
	concurrent_errors = verify_dashboard(current, latest)
	if concurrent_errors:
		print("Панель изменилась после предварительного чтения:", file=sys.stderr)
		for error in concurrent_errors:
			print("  - "+error, file=sys.stderr)
		raise MigrationError("Восстановление остановлено до записи. Запустите команду повторно.")
	current = latest
	expected = prepare_restore(current, backup_dashboard, target_widgetids)

	pre_restore = backup_document(
		current,
		zabbix_version=zabbix_version,
		api_url=api.url,
		target_widgetids=target_widgetids,
		operation="before-restore",
		options={"restored_from": str(args.restore)},
	)
	pre_restore_path = write_backup(pre_restore, args.backup_dir)
	print(f"Копия состояния перед восстановлением: {pre_restore_path}")

	update = build_update_payload(current, expected, target_widgetids, set(target_widgetids))
	api.call("dashboard.update", update)
	actual = fetch_dashboard(api, dashboardid)
	errors = verify_dashboard(expected, actual)
	if errors:
		for error in errors:
			print("  - "+error, file=sys.stderr)
		raise MigrationError("Проверка после восстановления не прошла.")
	print(f"Готово: {len(target_widgetids)} виджетов восстановлено; остальные не изменились.")


def main(argv: list[str] | None = None) -> int:
	parser = build_parser()
	args = parser.parse_args(argv)
	logged_in_session = False
	api: ZabbixApi | None = None

	try:
		sparkline_bounds = validate_arguments(args)
		if args.insecure:
			print("ПРЕДУПРЕЖДЕНИЕ: проверка HTTPS-сертификата отключена параметром --insecure.")
		api = ZabbixApi(args.api_url, timeout=args.timeout, insecure=args.insecure)
		zabbix_version = str(api.call("apiinfo.version", {}, authenticated=False))
		if not zabbix_version.startswith("7.4."):
			raise MigrationError(f"Поддерживается Zabbix 7.4.x; сервер сообщил версию {zabbix_version}.")
		print(f"Zabbix API: {zabbix_version} ({api.url})")
		logged_in_session = authenticate(api, args.username)

		if args.list:
			list_dashboards(api)
		elif args.restore is not None:
			run_restore(api, args, zabbix_version)
		else:
			run_migration(api, args, zabbix_version, sparkline_bounds)
		return 0
	except (MigrationError, KeyboardInterrupt) as error:
		message = "Операция прервана пользователем." if isinstance(error, KeyboardInterrupt) else str(error)
		print("ОШИБКА: "+message, file=sys.stderr)
		return 1
	finally:
		if logged_in_session and api is not None and api.token:
			try:
				api.call("user.logout", {})
			except MigrationError as error:
				print(f"ПРЕДУПРЕЖДЕНИЕ: API-сессию закрыть не удалось: {error}", file=sys.stderr)


if __name__ == "__main__":
	raise SystemExit(main())
