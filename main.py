"""Alfred: assistente comercial no Telegram, com Gemini e Google Agenda.

Opera a rota de segunda a sexta. Visitas podem ser registradas a qualquer momento
em que o processo estiver no ar. O computador precisa ficar ligado com este
script rodando para o relatório das 06:30 sair no horário.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import webbrowser
from datetime import datetime, timedelta
from enum import Enum
from html import escape
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask
from google import genai
from google.auth.transport.requests import Request
from google.genai import types
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from pydantic import BaseModel, Field
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import config

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("alfred")

WEEKDAYS = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")
DIARY_PREFIXES = ("resumo do dia", "fechamento do dia", "meu diário de hoje", "meu diario de hoje")

# Versículos curtos de encorajamento. A escolha do dia nunca repete a do dia anterior.
VERSES: tuple[tuple[str, str], ...] = (
    ("Josué 1:9", "Seja forte e corajoso. Não temas, porque o Senhor, teu Deus, é contigo por onde quer que andares."),
    ("Filipenses 4:13", "Tudo posso naquele que me fortalece."),
    ("Isaías 41:10", "Não temas, porque eu sou contigo; não te assombres, porque eu sou o teu Deus."),
    ("Salmos 23:1", "O Senhor é o meu pastor; nada me faltará."),
    ("Provérbios 3:5-6", "Confia no Senhor de todo o teu coração e não te estribes no teu próprio entendimento."),
    ("Romanos 8:31", "Se Deus é por nós, quem será contra nós?"),
    ("2 Timóteo 1:7", "Deus não nos deu o espírito de temor, mas de fortaleza, de amor e de moderação."),
    ("Salmos 37:5", "Entrega o teu caminho ao Senhor; confia nele, e ele o fará."),
    ("Mateus 11:28", "Vinde a mim, todos os que estais cansados e oprimidos, e eu vos aliviarei."),
    ("Jeremias 29:11", "Eu é que sei que pensamentos tenho a vosso respeito, diz o Senhor; pensamentos de paz e não de mal."),
    ("Salmos 121:2", "O meu socorro vem do Senhor, que fez o céu e a terra."),
    ("Isaías 40:31", "Os que esperam no Senhor renovam as suas forças, sobem com asas como águias."),
    ("Romanos 15:13", "O Deus de esperança vos encha de todo o gozo e paz na vossa fé."),
    ("Salmos 27:1", "O Senhor é a minha luz e a minha salvação; a quem temerei?"),
    ("1 Coríntios 16:13", "Vigiai, estai firmes na fé, portai-vos varonilmente e fortalecei-vos."),
    ("Deuteronômio 31:6", "Esforçai-vos e animai-vos; não temais, porque o Senhor teu Deus é contigo."),
    ("Salmos 46:1", "Deus é o nosso refúgio e fortaleza, socorro bem presente na angústia."),
    ("Filipenses 4:6-7", "Não andeis ansiosos por coisa alguma; em tudo, pela oração, sejam conhecidas diante de Deus as vossas petições."),
    ("Josué 1:8", "Não se aparte da tua boca o livro desta lei; medita nele dia e noite, para que tenhas bom êxito."),
    ("Salmos 16:8", "O Senhor tenho sempre à minha presença; estando ele à minha direita, não serei abalado."),
)

_calendar_lock = threading.Lock()
_state_lock = threading.RLock()
_morning_lock: asyncio.Lock | None = None
_calendar: dict[str, Any] = {}
_gemini: genai.Client | None = None


class Status(str, Enum):
    AUSENTE = "proprietario_ausente"
    OCUPADO = "dono_ocupado"
    LEAD = "lead_quente"
    AGUARDANDO = "aguardando_data"
    FECHADA = "venda_fechada"
    ROTINA = "retorno_rotina"


STATUS_META: dict[Status, dict[str, str]] = {
    Status.AUSENTE: {"color": "11", "emoji": "🔴", "label": "Proprietário ausente"},
    Status.OCUPADO: {"color": "6", "emoji": "🍊", "label": "Dono ocupado / reagendado"},
    Status.LEAD: {"color": "5", "emoji": "🟡", "label": "Lead quente"},
    Status.AGUARDANDO: {"color": "3", "emoji": "🟣", "label": "Aguardando definição de data"},
    Status.FECHADA: {"color": "10", "emoji": "🟢", "label": "Venda fechada no dia"},
    Status.ROTINA: {"color": "9", "emoji": "🔵", "label": "Retorno de rotina"},
}


class Intent(BaseModel):
    mode: str = Field(description="visita, mentor ou incompleto")
    transcript: str = ""
    clarification: str = ""


class VisitReport(BaseModel):
    classification: Status
    company: str = ""
    address: str = ""
    owner: str = ""
    argumentation: str = ""
    objections: str = ""
    next_step: str = ""
    perception: str = ""
    attendant_info: str = ""
    event_start: str = ""
    event_end: str = ""
    return_number_hint: int = 0
    needs_clarification: str = ""


class CalendarAction(BaseModel):
    classification: Status
    company: str = ""
    owner: str = ""
    address: str = ""
    objections: str = ""
    perception: str = ""
    attendant_info: str = ""
    next_step: str = ""
    event_start: str = ""
    event_end: str = ""
    detail: str = ""


class MentorReply(BaseModel):
    improvements: list[str] = Field(default_factory=list)
    book: str = ""
    chapter: str = ""
    application: str = ""
    calendar_actions: list[CalendarAction] = Field(default_factory=list)
    verse_reference: str = ""
    verse_text: str = ""


class ItemBrief(BaseModel):
    event_id: str
    briefing: str


class MorningBriefings(BaseModel):
    items: list[ItemBrief] = Field(default_factory=list)


def now_local() -> datetime:
    return datetime.now(config.TIMEZONE)


def h(value: object) -> str:
    text = str(value or "").strip()
    return escape(text) if text else "não informado"


def load_state() -> dict[str, Any]:
    with _state_lock:
        if not config.STATE_FILE.exists():
            return {}
        try:
            return json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("state.json ilegível; começando um estado novo.")
            return {}


def save_state(state: dict[str, Any]) -> None:
    with _state_lock:
        config.STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def update_state(mutator) -> dict[str, Any]:
    with _state_lock:
        state = load_state()
        mutator(state)
        save_state(state)
        return state


def gemini_client() -> genai.Client:
    global _gemini
    if _gemini is None:
        _gemini = genai.Client(api_key=config.GEMINI_API_KEY)
    return _gemini


def generate(contents: list[Any], schema: type[BaseModel]) -> BaseModel:
    response = gemini_client().models.generate_content(
        model=config.GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
        ),
    )
    parsed = response.parsed
    if isinstance(parsed, schema):
        return parsed
    if isinstance(parsed, dict):
        return schema.model_validate(parsed)
    text = (response.text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL).strip()
    return schema.model_validate_json(text)


def _save_token(creds: Credentials) -> None:
    try:
        config.GOOGLE_TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    except Exception as exc:
        log.warning("Não foi possível salvar o arquivo %s: %s", config.GOOGLE_TOKEN_FILE.name, exc)


def get_calendar():
    """Abre a Agenda e grava token.json para não pedir login de novo."""
    with _calendar_lock:
        creds: Credentials | None = _calendar.get("creds")

        # 1. Tenta carregar credenciais diretamente da variável de ambiente GOOGLE_TOKEN_JSON (ideal para nuvem/Render)
        token_env = os.environ.get("GOOGLE_TOKEN_JSON", "").strip()
        if creds is None and token_env:
            try:
                token_data = json.loads(token_env)
                creds = Credentials.from_authorized_user_info(token_data, config.CALENDAR_SCOPES)
                log.info("Credenciais do Google Agenda carregadas com sucesso via GOOGLE_TOKEN_JSON.")
            except Exception as exc:
                log.warning("Falha ao processar variável GOOGLE_TOKEN_JSON: %s", exc)

        # 2. Se não houver na variável, tenta carregar do arquivo local token.json
        if creds is None and config.GOOGLE_TOKEN_FILE.exists():
            creds = Credentials.from_authorized_user_file(
                str(config.GOOGLE_TOKEN_FILE), config.CALENDAR_SCOPES
            )

        if creds and creds.valid:
            service = _calendar.get("service")
            if service is None:
                service = build("calendar", "v3", credentials=creds, cache_discovery=False)
                _calendar["creds"] = creds
                _calendar["service"] = service
            return service

        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _save_token(creds)
        else:
            if not config.GOOGLE_CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"Não encontrei {config.GOOGLE_CREDENTIALS_FILE.name} na pasta do projeto."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(config.GOOGLE_CREDENTIALS_FILE), config.CALENDAR_SCOPES
            )
            try:
                creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
            except (webbrowser.Error, Exception) as exc:
                auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
                msg = (
                    "\n============================================================\n"
                    "[AUTENTICAÇÃO GOOGLE AGENDA NECESSÁRIA]\n"
                    "O navegador não pôde ser aberto automaticamente (ambiente em nuvem/Render).\n"
                    "Copie o link abaixo e cole no navegador para autorizar:\n\n"
                    f"{auth_url}\n\n"
                    "DICA PARA O RENDER:\n"
                    "1. Execute o bot uma vez na sua máquina local para gerar o arquivo 'token.json'.\n"
                    "2. Abra o arquivo 'token.json', copie todo o conteúdo.\n"
                    "3. No Render, crie uma variável de ambiente chamada GOOGLE_TOKEN_JSON\n"
                    "   e cole esse conteúdo. Assim, o robô não dependerá de navegador na nuvem.\n"
                    "============================================================\n"
                )
                print(msg)
                log.error(msg)
                if hasattr(flow, "run_console"):
                    try:
                        creds = flow.run_console(access_type="offline", prompt="consent")
                    except Exception:
                        raise exc
                else:
                    raise exc

            _save_token(creds)

        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        _calendar["creds"] = creds
        _calendar["service"] = service
        return service


def _execute(request):
    with _calendar_lock:
        result = request.execute()
        creds = _calendar.get("creds")
        if creds is not None:
            _save_token(creds)
        return result


def parse_local(value: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        else:
            parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=config.TIMEZONE)
    return parsed.astimezone(config.TIMEZONE)


def event_start(event: dict[str, Any]) -> datetime | None:
    start = event.get("start") or {}
    if start.get("dateTime"):
        return parse_local(start["dateTime"])
    if start.get("date"):
        day = datetime.fromisoformat(start["date"])
        return day.replace(tzinfo=config.TIMEZONE)
    return None


def company_of(event: dict[str, Any]) -> str:
    private = (event.get("extendedProperties") or {}).get("private") or {}
    if private.get("company"):
        return str(private["company"]).strip()
    summary = str(event.get("summary") or "")
    if ":" in summary:
        return summary.split(":")[-1].strip()
    return summary.strip()


def list_events(start: datetime, end: datetime, query: str = "") -> list[dict[str, Any]]:
    service = get_calendar()
    payload: dict[str, Any] = {
        "calendarId": config.CALENDAR_ID,
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": 50,
    }
    if query:
        payload["q"] = query
    result = _execute(service.events().list(**payload))
    return list(result.get("items") or [])


def events_for_company(company: str) -> list[dict[str, Any]]:
    if not company.strip():
        return []
    window_start = now_local() - timedelta(days=365)
    window_end = now_local() + timedelta(days=365)
    found = list_events(window_start, window_end, company)
    needle = company.casefold()
    return [item for item in found if needle in company_of(item).casefold() or needle in str(item.get("summary", "")).casefold()]


def previous_visits(company: str, start: datetime, ignore_id: str | None = None) -> int:
    total = 0
    for item in events_for_company(company):
        if ignore_id and item.get("id") == ignore_id:
            continue
        when = event_start(item)
        if when and when < start:
            total += 1
    return total


def return_number(company: str, hint: int, start: datetime, ignore_id: str | None = None) -> int:
    prior = previous_visits(company, start, ignore_id)
    counted = prior + 1 if prior else 2
    if hint and hint >= 2:
        return max(counted, hint)
    return counted


def build_title(report: VisitReport, count: int) -> str:
    company = report.company.strip()
    if report.classification == Status.AUSENTE:
        return f"[PROSPECÇÃO] Retorno: {company}"
    if report.classification == Status.OCUPADO:
        return f"[REAGENDADO] Voltar na Empresa: {company}"
    if report.classification == Status.LEAD:
        return f"[LEAD QUENTE] Decisão Sócios: {company}"
    if report.classification == Status.AGUARDANDO:
        return f"[AGUARDANDO DATA] Fechamento: {company}"
    if report.classification == Status.FECHADA:
        return f"🎉 PALESTRA AGENDADA: {company}"
    return f"[{count}º Retorno] Empresa: {company}"


def _filled(value: str, fallback: str) -> str:
    text = (value or "").strip()
    return text if text else fallback


def build_notes(report: VisitReport) -> str:
    owner = _filled(report.owner, "não informado")
    if report.classification == Status.AUSENTE:
        info = _filled(report.attendant_info, "não informado")
        return f"Dono: {owner}. Informações do atendente: {info}"
    if report.classification == Status.OCUPADO:
        return f"Dono {owner} estava muito atarefado. Pediu retorno especificamente para este horário"
    if report.classification == Status.LEAD:
        objections = _filled(report.objections, "nenhuma anotada")
        perception = _filled(report.perception, "não informada")
        return (
            f"Apresentado o treinamento de {config.OFFER_PRICE}. "
            f"Sócio presente: {owner}. Objeções/Dúvidas anotadas: {objections}. "
            f"Percepção: {perception}. Aguardando validação do outro decisor"
        )
    if report.classification == Status.AGUARDANDO:
        return (
            "Treinamento aceito. Falta definir o dia oficial da palestra. "
            "Retornar para coletar a data e assinar o contrato"
        )
    if report.classification == Status.FECHADA:
        return (
            f"CONTRATO FECHADO. Valor: {config.OFFER_PRICE}. {config.CLT_CLAUSE}"
        )
    return "Acompanhamento contínuo de prospecção para tentar o fechamento"


def same_slot(events: list[dict[str, Any]], company: str, start: datetime) -> dict[str, Any] | None:
    needle = company.casefold()
    for item in events:
        if needle not in company_of(item).casefold():
            continue
        current = event_start(item)
        if current and abs((current - start).total_seconds()) < 60:
            return item
    return None


def upsert_event(report: VisitReport) -> tuple[str, str, bool]:
    """Cria ou atualiza o evento. Devolve link, título e se já existia."""
    start = parse_local(report.event_start)
    if start is None or not report.company.strip():
        raise ValueError("Empresa ou horário ausente.")
    end = parse_local(report.event_end) if report.event_end else None
    if end is None or end <= start:
        minutes = (
            config.LECTURE_DURATION_MINUTES
            if report.classification == Status.FECHADA
            else config.VISIT_DURATION_MINUTES
        )
        end = start + timedelta(minutes=minutes)
    related = events_for_company(report.company)
    existing = same_slot(related, report.company, start)
    count = return_number(
        report.company,
        report.return_number_hint,
        start,
        existing.get("id") if existing else None,
    )
    title = build_title(report, count)
    notes = build_notes(report)
    body: dict[str, Any] = {
        "summary": title,
        "description": notes,
        "start": {"dateTime": start.isoformat(), "timeZone": "America/Sao_Paulo"},
        "end": {"dateTime": end.isoformat(), "timeZone": "America/Sao_Paulo"},
        "colorId": STATUS_META[report.classification]["color"],
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": config.REMINDER_MINUTES}],
        },
        "extendedProperties": {
            "private": {
                "alfred": "1",
                "company": report.company.strip(),
                "status": report.classification.value,
                "returnNumber": str(count) if report.classification == Status.ROTINA else "",
            }
        },
    }
    address = report.address.strip()
    if address:
        body["location"] = address

    service = get_calendar()
    if existing:
        updated = _execute(
            service.events().patch(
                calendarId=config.CALENDAR_ID,
                eventId=existing["id"],
                body=body,
            )
        )
        return updated.get("htmlLink") or "", title, True
    created = _execute(service.events().insert(calendarId=config.CALENDAR_ID, body=body))
    return created.get("htmlLink") or "", title, False


def recent_context(company: str = "") -> str:
    start = now_local() - timedelta(days=45)
    end = now_local() + timedelta(days=60)
    events = list_events(start, end, company) if company else list_events(start, end)
    lines = []
    for item in events[:20]:
        when = event_start(item)
        stamp = when.strftime("%d/%m/%Y %H:%M") if when else "sem horário"
        lines.append(
            f"- {stamp} | {item.get('summary', '')} | {item.get('description', '')}"
        )
    return "\n".join(lines) if lines else "Nenhum evento recente do Alfred."


def clock_context() -> str:
    moment = now_local()
    return (
        f"Agora: {moment.strftime('%Y-%m-%d %H:%M')} "
        f"({WEEKDAYS[moment.weekday()]}), fuso America/Sao_Paulo."
    )


VISIT_RULES = f"""
Você é o Alfred, assistente comercial de visitas presenciais. Extraia somente o que foi dito.
Não invente nome, endereço, objeção nem horário. Se faltar um dado, deixe o campo vazio.
Oferta fixa do vendedor: treinamento de {config.OFFER_PRICE}, com {config.CLT_CLAUSE}.

Classifique a visita em exatamente uma destas situações:
- proprietario_ausente: o dono não estava; o atendente passou os melhores dias e horários. event_start é o retorno sugerido.
- dono_ocupado: o dono foi encontrado, estava atarefado e pediu para voltar ou ligar em um momento específico. event_start é esse momento.
- lead_quente: a oferta foi apresentada a um sócio, ele gostou, mas depende de outro sócio ou da esposa. event_start é o retorno para acompanhar a decisão.
- aguardando_data: o dono aceitou {config.OFFER_PRICE} e a cláusula do Art. 462 da CLT, mas ainda vai definir o dia da palestra. event_start é o retorno para cobrar essa data, não a palestra.
- venda_fechada: contrato fechado ou data da palestra definida na hora. event_start é o dia e a hora da palestra. Não é revisita.
- retorno_rotina: o processo se arrasta e esta é a 2ª, 3ª visita ou mais para tentar fechar. event_start é o próximo retorno.

event_start e event_end no formato YYYY-MM-DDTHH:MM:SS, horário local, sem fuso. Vazio se o horário não foi dito.
Calcule "amanhã", "quarta" e "semana que vem" a partir da data atual. Não chute um horário redondo.
return_number_hint: se o vendedor disser que é o 3º retorno, grave 3; senão null.
needs_clarification: frase curta se faltar o nome da empresa ou o dia/horário do evento. Senão vazio.
""".strip()


def read_intent(text: str, audio: bytes | None, mime: str) -> Intent:
    instructions = (
        "Classifique a mensagem do vendedor. "
        "mode=mentor se ele começar o relato de fechamento do dia com "
        "'Resumo do dia', 'Fechamento do dia' ou 'Meu diário de hoje', ou se o áudio inteiro for esse diário. "
        "mode=visita se ele acabou de sair de uma empresa e relata a visita. "
        "mode=incompleto se não der para saber o que ele quer. "
        "transcript é o texto fiel da mensagem, em português. "
        "clarification pede o que falta só quando mode=incompleto.\n\n"
        + clock_context()
    )
    contents: list[Any] = [instructions]
    if audio:
        contents.append(types.Part.from_bytes(data=audio, mime_type=mime))
    else:
        contents.append(text)
    return generate(contents, Intent)  # type: ignore[return-value]


def extract_visit(text: str, audio: bytes | None, mime: str) -> VisitReport:
    state = load_state()
    pending = state.get("pending_visit") or {}
    instructions = "\n\n".join(
        [
            VISIT_RULES,
            clock_context(),
            "Histórico recente da agenda:\n" + recent_context(),
            "Visita pendente, se a mensagem nova só completar horário ou um dado que faltou:\n"
            + json.dumps(pending, ensure_ascii=False),
            "Se a mensagem for outra empresa, ignore a visita pendente e extraia a nova.",
            "Mensagem de texto do vendedor:\n" + (text or "(veio só áudio)"),
        ]
    )
    contents: list[Any] = [instructions]
    if audio:
        contents.append(types.Part.from_bytes(data=audio, mime_type=mime))
    return generate(contents, VisitReport)  # type: ignore[return-value]


def agenda_link(url: str) -> str:
    if not url:
        return "não consegui obter o link"
    return f'<a href="{escape(url, quote=True)}">abrir no Google Agenda</a>'


def format_visit_card(report: VisitReport, link: str, existed: bool, title: str) -> str:
    meta = STATUS_META[report.classification]
    agenda = agenda_link(link) if link else "não criei o evento"
    if link and existed:
        status_line = "Atualizei o evento que já estava nesse horário."
    elif link:
        status_line = "Evento criado na agenda."
    else:
        status_line = ""
    lines = [
        f"{meta['emoji']} <b>{escape(meta['label'])}</b>",
        escape(title) if title else "",
        escape(status_line) if status_line else "",
        "",
        f"<b>Empresa:</b> {h(report.company)}",
        f"<b>Endereço:</b> {h(report.address)}",
        f"<b>Dono:</b> {h(report.owner)}",
        f"<b>Argumentação:</b> {h(report.argumentation)}",
        f"<b>Objeções:</b> {h(report.objections)}",
        f"<b>Próximo passo:</b> {h(report.next_step)}",
        f"<b>Agenda:</b> {agenda}",
    ]
    if report.needs_clarification and not link:
        lines.extend(["", escape(report.needs_clarification)])
    return "\n".join(line for line in lines if line)


def apply_visit(report: VisitReport) -> str:
    missing = []
    if not report.company.strip():
        missing.append("o nome da empresa")
    if parse_local(report.event_start) is None:
        missing.append("o dia e o horário")
    if missing:
        payload = report.model_dump(mode="json")
        update_state(lambda state: state.__setitem__("pending_visit", payload))
        report.needs_clarification = (
            "Falta " + " e ".join(missing) + ". Responda só com esse dado que eu marco na agenda."
        )
        return format_visit_card(report, "", False, "")

    link, title, existed = upsert_event(report)
    update_state(lambda state: state.pop("pending_visit", None))
    return format_visit_card(report, link, existed, title)


MENTOR_RULES = f"""
Você é o Alfred no modo Mentor Estratégico, no fim do expediente do vendedor.
Cruze o relato com as técnicas de "Como Fazer Amigos e Influenciar Pessoas" (Dale Carnegie)
e "Como Convencer Alguém em 90 Segundos" (Nicholas Boothman).
Não copie trechos dos livros. Cite um capítulo real e explique, com suas palavras, como aplicar amanhã.

Devolva:
- improvements: 2 ou 3 pontos concretos de melhoria da abordagem deste dia.
- book e chapter: um dos dois livros, o capítulo mais útil para o erro principal do dia.
- application: como usar essa técnica na próxima visita.
- calendar_actions: só fatos novos que o vendedor disse ter esquecido de marcar.
  Cada ação precisa de empresa e event_start (YYYY-MM-DDTHH:MM:SS, local). Sem horário, não inclua a ação.
  Use as mesmas seis classificações das visitas. Exemplo: o sócio pediu para voltar quarta às 10h
  porque estava sem tempo -> dono_ocupado. Processo que já vai longe -> retorno_rotina.
- verse_reference e verse_text: um versículo bíblico curto de encorajamento, em português.
  Não use a referência proibida indicada abaixo.

Oferta do vendedor, se precisar registrar venda ou lead: {config.OFFER_PRICE}. {config.CLT_CLAUSE}.
""".strip()


def verse_for_today(suggested_ref: str, suggested_text: str) -> tuple[str, str]:
    chosen: dict[str, str] = {}

    def pick(state: dict[str, Any]) -> None:
        previous = str(state.get("last_verse_ref") or "")
        index = now_local().date().toordinal() % len(VERSES)
        ref, text = VERSES[index]
        if ref == previous:
            ref, text = VERSES[(index + 1) % len(VERSES)]
        if suggested_ref.strip() and suggested_text.strip() and suggested_ref.strip() != previous:
            ref, text = suggested_ref.strip(), suggested_text.strip()
        state["last_verse_ref"] = ref
        state["last_verse_date"] = now_local().date().isoformat()
        chosen["ref"] = ref
        chosen["text"] = text

    update_state(pick)
    return chosen["ref"], chosen["text"]


def mentor(text: str, audio: bytes | None, mime: str) -> str:
    state = load_state()
    previous_verse = str(state.get("last_verse_ref") or "nenhum")
    instructions = "\n\n".join(
        [
            MENTOR_RULES,
            clock_context(),
            f"Não repita este versículo: {previous_verse}.",
            "Agenda recente:\n" + recent_context(),
            "Relato:\n" + (text or "(veio só áudio)"),
        ]
    )
    contents: list[Any] = [instructions]
    if audio:
        contents.append(types.Part.from_bytes(data=audio, mime_type=mime))
    reply: MentorReply = generate(contents, MentorReply)  # type: ignore[assignment]

    agenda_lines: list[str] = []
    for action in reply.calendar_actions:
        if not action.company.strip() or parse_local(action.event_start) is None:
            continue
        report = VisitReport(
            classification=action.classification,
            company=action.company,
            address=action.address,
            owner=action.owner,
            objections=action.objections,
            perception=action.perception,
            attendant_info=action.attendant_info,
            next_step=action.next_step or action.detail,
            event_start=action.event_start,
            event_end=action.event_end,
        )
        try:
            link, title, existed = upsert_event(report)
        except Exception:
            log.exception("Falha ao gravar ação do diário para %s", action.company)
            agenda_lines.append(f"Não consegui gravar {escape(action.company)} na agenda.")
            continue
        verb = "Atualizei" if existed else "Marquei"
        agenda_lines.append(f"{verb}: {escape(title)}. {agenda_link(link)}")

    if not agenda_lines:
        agenda_lines.append("Nenhuma informação nova com dia e horário para lançar na agenda.")

    improvements = reply.improvements or ["Não identifiquei um ponto específico. Conte como o cliente reagiu à oferta."]
    ref, verse = verse_for_today(reply.verse_reference, reply.verse_text)
    improvement_lines = "\n".join(f"• {escape(item)}" for item in improvements)
    return "\n".join(
        [
            "<b>Mentor estratégico</b>",
            "",
            "📉 <b>Pontos de melhoria</b>",
            improvement_lines,
            "",
            "📚 <b>Leitura para amanhã</b>",
            f"<b>Livro:</b> {h(reply.book)}",
            f"<b>Capítulo:</b> {h(reply.chapter)}",
            f"<b>Como aplicar:</b> {h(reply.application)}",
            "",
            "🔄 <b>Agenda</b>",
            "\n".join(agenda_lines),
            "",
            f"⛪ {escape(verse)} — {escape(ref)}",
        ]
    )


def day_bounds(day: datetime) -> tuple[datetime, datetime]:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def history_before(company: str, moment: datetime) -> list[dict[str, Any]]:
    past = []
    for item in events_for_company(company):
        when = event_start(item)
        if when and when < moment:
            past.append(item)
    past.sort(key=lambda item: event_start(item) or moment)
    return past[-5:]


def fallback_briefing(event: dict[str, Any]) -> str:
    when = event_start(event)
    hour = when.strftime("%H:%M") if when else "--:--"
    title = str(event.get("summary") or "compromisso")
    company = company_of(event)
    past = history_before(company, when) if when else []
    if not past:
        return f"Às {hour} você tem {title}. Não há visita anterior registrada na agenda."
    last = past[-1]
    last_when = event_start(last)
    last_stamp = last_when.strftime("%d/%m %H:%M") if last_when else "na visita anterior"
    description = str(last.get("description") or "sem observações anotadas")
    return f"Às {hour} você tem {title}. Na última visita ({last_stamp}): {description}"


def morning_message() -> str:
    moment = now_local()
    start, end = day_bounds(moment)
    events = list_events(start, end)
    header = f"☀️ <b>Rota de {WEEKDAYS[moment.weekday()]}, {moment.strftime('%d/%m/%Y')}</b>"
    if not events:
        return header + "\n\nNenhum compromisso na agenda hoje. O dia está livre para prospecção nova."

    facts = []
    for item in events:
        when = event_start(item)
        past = history_before(company_of(item), when) if when else []
        facts.append(
            {
                "event_id": item.get("id", ""),
                "time": when.strftime("%H:%M") if when else "",
                "title": item.get("summary", ""),
                "description": item.get("description", ""),
                "past": [
                    {
                        "when": event_start(old).strftime("%d/%m/%Y %H:%M") if event_start(old) else "",
                        "title": old.get("summary", ""),
                        "description": old.get("description", ""),
                    }
                    for old in past
                ],
            }
        )
    briefings: dict[str, str] = {}
    try:
        prompt = (
            "Escreva um briefing curto, em português, para cada compromisso do vendedor hoje. "
            "Use só os fatos enviados. Não invente objeção, nome nem visita. "
            "O texto deve começar no estilo: 'Às HH:MM você tem ...'. "
            "Se houver visita anterior, diga o que aconteceu na última, com base na descrição.\n\n"
            + json.dumps(facts, ensure_ascii=False)
        )
        parsed: MorningBriefings = generate([prompt], MorningBriefings)  # type: ignore[assignment]
        briefings = {item.event_id: item.briefing.strip() for item in parsed.items if item.briefing.strip()}
    except Exception:
        log.exception("Gemini não montou a rota; usando o histórico cru da agenda.")

    blocks = [header, ""]
    for item, fact in zip(events, facts):
        when = event_start(item)
        hour = when.strftime("%H:%M") if when else "--:--"
        color = str(item.get("colorId") or "")
        emoji = next((meta["emoji"] for meta in STATUS_META.values() if meta["color"] == color), "•")
        title = str(item.get("summary") or "Compromisso")
        text = briefings.get(str(item.get("id"))) or fallback_briefing(item)
        blocks.append(f"{emoji} <b>{escape(hour)}</b> — {escape(title)}")
        blocks.append(escape(text))
        blocks.append("")
    return "\n".join(blocks).strip()


async def send_chunks(bot, chat_id: int, text: str) -> None:
    chunks = [text[index : index + 4000] for index in range(0, len(text), 4000)] or [text]
    for chunk in chunks:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            log.exception("Falha ao enviar HTML; reenviando como texto.")
            await bot.send_message(chat_id=chat_id, text=re.sub(r"<[^>]+>", "", chunk))


def allowed_chat(chat_id: int) -> bool:
    if config.TELEGRAM_CHAT_ID is not None:
        return chat_id == config.TELEGRAM_CHAT_ID
    owner = load_state().get("chat_id")
    if owner is None:
        update_state(lambda state: state.setdefault("chat_id", chat_id))
        log.info("Chat %s definido como dono do Alfred.", chat_id)
        return True
    return int(owner) == chat_id


def owner_chat_id() -> int | None:
    if config.TELEGRAM_CHAT_ID is not None:
        return config.TELEGRAM_CHAT_ID
    owner = load_state().get("chat_id")
    return int(owner) if owner is not None else None


def is_diary(text: str) -> bool:
    lowered = text.strip().casefold()
    return any(lowered.startswith(prefix) for prefix in DIARY_PREFIXES)


async def run_blocking(func, *args):
    return await asyncio.to_thread(func, *args)


def _morning_guard() -> asyncio.Lock:
    global _morning_lock
    if _morning_lock is None:
        _morning_lock = asyncio.Lock()
    return _morning_lock


async def maybe_send_morning(application: Application, force: bool = False) -> None:
    async with _morning_guard():
        moment = now_local()
        state = load_state()
        if not force:
            if moment.weekday() >= 5:
                return
            if state.get("last_morning_report_date") == moment.date().isoformat():
                return
            if moment.hour < 6 or (moment.hour == 6 and moment.minute < 30):
                return
        chat_id = owner_chat_id()
        if chat_id is None:
            log.info("Relatório matinal adiado: ninguém falou com o Alfred ainda.")
            return
        try:
            text = await run_blocking(morning_message)
        except Exception:
            log.exception("Falha ao montar a rota da manhã.")
            text = "Não consegui ler a agenda agora. Confira a conexão e me chame de novo com /rota."
        await send_chunks(application.bot, chat_id, text)
        if not force:
            today = moment.date().isoformat()
            update_state(lambda current: current.__setitem__("last_morning_report_date", today))


async def download_audio(message) -> tuple[bytes | None, str]:
    media = message.voice or message.audio
    if media is None:
        return None, "audio/ogg"
    telegram_file = await media.get_file()
    payload = bytes(await telegram_file.download_as_bytearray())
    mime = (getattr(media, "mime_type", None) or "audio/ogg").split(";")[0].strip() or "audio/ogg"
    return payload, mime


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or not allowed_chat(update.effective_chat.id):
        return
    await update.effective_message.reply_text(
        "Alfred no ar, de segunda a sexta.\n\n"
        "Depois de cada visita, mande um áudio ou um texto. Eu classifico em uma das seis situações, "
        "marco a cor na agenda e devolvo a ficha com o link do evento.\n\n"
        "Às 06:30, nos dias úteis, eu envio a rota do dia.\n"
        "À noite, comece a mensagem com \"Resumo do dia\", \"Fechamento do dia\" ou "
        "\"Meu diário de hoje\" para a mentoria.\n\n"
        "/rota mostra a rota de hoje agora."
    )
    await maybe_send_morning(context.application)


async def on_route(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or not allowed_chat(update.effective_chat.id):
        return
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    await maybe_send_morning(context.application, force=True)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None or not allowed_chat(chat.id):
        return
    await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
    text = message.text or message.caption or ""
    audio, mime = await download_audio(message)

    try:
        if text and is_diary(text):
            answer = await run_blocking(mentor, text, audio, mime)
        elif audio:
            intent = await run_blocking(read_intent, text, audio, mime)
            mode = intent.mode.strip().casefold()
            if "mentor" in mode or is_diary(intent.transcript):
                answer = await run_blocking(mentor, intent.transcript or text, audio, mime)
            elif mode == "incompleto":
                answer = intent.clarification or "Não entendi o relato. Diga a empresa e o que aconteceu na visita."
            else:
                report = await run_blocking(extract_visit, intent.transcript or text, audio, mime)
                answer = await run_blocking(apply_visit, report)
        else:
            report = await run_blocking(extract_visit, text, None, mime)
            answer = await run_blocking(apply_visit, report)
    except Exception:
        log.exception("Falha ao processar mensagem.")
        answer = "Não consegui processar agora. Tente de novo em instantes."
    await send_chunks(context.bot, chat.id, answer)
    await maybe_send_morning(context.application)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Erro não tratado.", exc_info=context.error)


async def post_init(application: Application) -> None:
    scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)
    scheduler.add_job(
        maybe_send_morning,
        CronTrigger(day_of_week="mon-fri", hour=6, minute=30, timezone=config.TIMEZONE),
        kwargs={"application": application},
        id="morning_report",
        replace_existing=True,
        misfire_grace_time=3 * 60 * 60,
        coalesce=True,
    )
    scheduler.start()
    application.bot_data["scheduler"] = scheduler
    await maybe_send_morning(application)


async def post_shutdown(application: Application) -> None:
    scheduler = application.bot_data.get("scheduler")
    if scheduler is not None:
        scheduler.shutdown(wait=False)


flask_app = Flask(__name__)


@flask_app.route("/")
def health_check() -> str:
    return "Alfred está online!"


def run_web_server() -> None:
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


def start_web_server() -> None:
    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()


def main() -> None:
    start_web_server()
    get_calendar()
    application = (
        Application.builder()
        .token(config.TELEGRAM_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    private = filters.ChatType.PRIVATE
    application.add_handler(CommandHandler("start", on_start, filters=private))
    application.add_handler(CommandHandler("rota", on_route, filters=private))
    application.add_handler(
        MessageHandler((filters.TEXT | filters.VOICE | filters.AUDIO) & private & ~filters.COMMAND, on_message)
    )
    application.add_error_handler(on_error)
    log.info("Alfred ouvindo o Telegram. Relatório às 06:30, segunda a sexta.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
