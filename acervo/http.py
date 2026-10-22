from __future__ import annotations

import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from protego import Protego

USER_AGENT = "ConcursoEstudosPessoais/2.0"
HOSTS = {"www.pciconcursos.com.br", "pciconcursos.com.br", "fafipa.selecao.net.br", "concursos.fundacaofafipa.org.br", "concursos.fundacaounespar.org.br", "anexos-r2.selecao.net.br", "anexos.cdn.selecao.net.br", "anexos.selecao.net.br"}


class AccessRequired(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


class SourceFailure(RuntimeError):
    """A known, classified response failure from a cataloged source."""
    code = "falha_processamento"
    origin_state = "falha de processamento"
    human_action = False

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class HumanActionRequired(SourceFailure):
    code = "requer_intervencao"
    origin_state = "intervenção necessária"
    human_action = True


class SourceUnavailable(SourceFailure):
    code = "indisponivel_na_fonte"
    origin_state = "indisponível na fonte"


class TemporaryFailure(SourceFailure):
    code = "falha_temporaria"
    origin_state = "falha temporária"


class ProcessingFailure(SourceFailure):
    code = "falha_processamento"
    origin_state = "falha de processamento"


class Client:
    def __init__(self, interval=2.5, check=None):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.interval = max(2.5, interval)
        self.last = {}
        self.robots = {}
        self.check = check or (lambda: None)

    def sleep(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.check()
            time.sleep(min(.25, max(0, end - time.monotonic())))

    def validate(self, url):
        p = urlparse(url)
        if p.scheme != "https" or p.hostname not in HOSTS or p.username or p.password or p.port not in (None, 443):
            raise AccessRequired("Domínio ou protocolo não autorizado: " + url)

    def _request(self, url, stream=False, robots=False, purpose="discovery", referer=None):
        self.validate(url)
        for _ in range(6):
            self.check()
            host = urlparse(url).netloc
            if not robots and purpose == "discovery":
                self.allowed(url)
            self.sleep(max(0, self.interval - (time.monotonic() - self.last.get(host, 0))))
            self.last[host] = time.monotonic()
            headers = {"Referer": referer} if referer else None
            response = self.session.get(url, stream=stream, timeout=(10, 35), allow_redirects=False, headers=headers)
            if response.is_redirect:
                target = urljoin(url, response.headers.get("Location", ""))
                response.close()
                self.validate(target)
                url = target
                continue
            return response
        raise AccessRequired("Muitos redirecionamentos")

    def allowed(self, url):
        p = urlparse(url)
        base = f"https://{p.netloc}"
        if base not in self.robots:
            with self._request(base + "/robots.txt", robots=True) as r:
                if r.status_code == 404:
                    text = ""
                else:
                    if r.status_code in (401, 403):
                        raise AccessRequired("Não foi possível validar robots.txt: " + base)
                    r.raise_for_status()
                    text = r.text
                    if "<html" in text.lower():
                        raise AccessRequired("robots.txt retornou HTML: " + base)
                self.robots[base] = Protego.parse(text)
        rules = self.robots[base]
        if not rules.can_fetch(url, USER_AGENT):
            raise AccessRequired("Coleta automática não permitida pelo robots.txt; abra a origem e importe o arquivo.")
        crawl_delay = rules.crawl_delay(USER_AGENT)
        if crawl_delay:
            self.interval = max(self.interval, float(crawl_delay))

    def get(self, url, stream=False, *, purpose="discovery", referer=None):
        if purpose not in {"discovery", "document"}:
            raise ValueError("Finalidade da requisição desconhecida")
        if purpose == "document" and not urlparse(url).path.lower().endswith(".pdf"):
            raise ValueError("Somente documentos PDF catalogados podem ser baixados")
        for attempt in range(3):
            try:
                r = self._request(url, stream, purpose=purpose, referer=referer)
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 2:
                    raise TemporaryFailure("Falha de conexão após três tentativas: " + url) from None
                self.sleep(3 * 2**attempt)
                continue
            if r.status_code in (401, 403):
                status = r.status_code
                r.close()
                if purpose == "document":
                    raise HumanActionRequired("A fonte recusou o acesso (HTTP " + str(status) + "). Abra a origem e conclua o acesso necessário.", status)
                raise AccessRequired("Acesso restrito ou verificação necessária: " + url)
            if r.status_code in (404, 410):
                status = r.status_code
                r.close()
                raise SourceUnavailable("O documento não está mais disponível na fonte (HTTP " + str(status) + ")", status)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                delay = r.headers.get("Retry-After", "")
                r.close()
                self.sleep(min(60, int(delay)) if delay.isdigit() else 3 * 2**attempt)
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                status = r.status_code
                r.close()
                raise TemporaryFailure("A fonte continua indisponível após três tentativas (HTTP " + str(status) + ")", status)
            try:
                r.raise_for_status()
            except requests.HTTPError as e:
                status = r.status_code
                r.close()
                raise SourceFailure("Erro HTTP ao acessar o documento: " + str(e), status) from e
            return r
        raise RuntimeError("Falha de rede")

    def html(self, url):
        with self.get(url) as r:
            return BeautifulSoup(r.content, "html.parser")
