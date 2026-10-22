"""CLI do acervo. A interface Docker e este comando usam os mesmos serviços."""
import argparse
import os
from pathlib import Path

from .db import initialize
from .jobs import Runner, create_job
from .model import DEFAULT_ROLES, EXTRA_ROLES, Filters


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--saida", type=Path, default=Path(os.environ.get("ACERVO_DIR", "acervo_pci")))
    p.add_argument("--download", action="store_true", help="Catalogar e baixar documentos públicos")
    p.add_argument("--somente-download", action="store_true", help="Reprocessar documentos já catalogados")
    p.add_argument("--correlatas", action="store_true", help="Incluir cargos administrativos de apoio")
    p.add_argument("--cargo", action="append", help="Pode repetir; omitir usa o perfil inicial")
    p.add_argument("--todos-cargos", action="store_true")
    p.add_argument("--banca", action="append", default=[])
    p.add_argument("--orgao", action="append", default=[])
    p.add_argument("--municipio", action="append", default=[])
    p.add_argument("--uf", action="append", default=[])
    p.add_argument("--fonte", action="append", choices=["PCI", "FAFIPA"])
    p.add_argument("--tipo", action="append", default=[])
    p.add_argument("--ano-inicio", type=int)
    p.add_argument("--ano-fim", type=int)
    p.add_argument("--foco", action="store_true", help="FAFIPA ou Prefeitura de Curitiba")
    p.add_argument("--intervalo", type=float, default=2.5)
    args = p.parse_args()
    if args.intervalo < 2.5:
        p.error("O intervalo mínimo é 2,5 segundos")
    if args.ano_inicio and args.ano_fim and args.ano_inicio > args.ano_fim:
        p.error("Ano inicial deve ser menor ou igual ao final")
    initialize(args.saida)
    f = Filters(cargos=[] if args.todos_cargos else args.cargo or DEFAULT_ROLES + (EXTRA_ROLES if args.correlatas else []), bancas=args.banca, orgaos=args.orgao, municipios=args.municipio, ufs=args.uf, fontes=args.fonte or ["PCI", "FAFIPA"], tipos=args.tipo, ano_inicio=args.ano_inicio, ano_fim=args.ano_fim, foco=args.foco)
    action = "download" if args.somente_download else "catalogar"
    try:
        job = create_job(action, {"filtros": f.to_dict(), "download": args.download}, args.saida, launch=False)
    except ValueError as e:
        p.error(str(e))
    runner = Runner(job, args.saida)
    runner.client.interval = args.intervalo
    try:
        state = runner.run()
    except KeyboardInterrupt:
        from .db import connect, now
        with connect(args.saida) as c:
            c.execute("UPDATE execucoes SET estado='interrompida',atualizado_em=? WHERE id=?", (now(), job))
        raise SystemExit(130)
    print("Resultado:", state, "| Saída:", args.saida.resolve())
    raise SystemExit(0 if state == "concluída" else 2)
