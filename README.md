# Acervo de provas

Aplicação pessoal local para reunir provas e gabaritos de concursos, com foco inicial em Analista Administrativo, Prefeitura de Curitiba e FAFIPA.

## Instalação no Windows

Instale e abra o Docker Desktop. No PowerShell, clone o repositório e entre na pasta:

```powershell
git clone https://github.com/Onlytb/baixar_provas_e_gabaritos_concursos.git
cd baixar_provas_e_gabaritos_concursos
```

Depois siga a seção **Iniciar** abaixo. A pasta `acervo_pci/` é local e fica fora do GitHub; cada pessoa mantém seu próprio banco e PDFs.

## Iniciar

Com o Docker Desktop funcionando, abra o PowerShell nesta pasta:

```powershell
docker compose up --build -d
```

Acesse **http://localhost:8501**. A porta é publicada apenas em `127.0.0.1`.

```powershell
docker compose ps
docker compose logs --tail 80 acervo
docker compose stop
docker compose start
```

## Usar

1. **Buscar provas:** ajuste os filtros à esquerda e clique em **Pesquisar e catalogar**. A busca usa PCI e Qconcursos por padrão. O foco inicial considera os cargos escolhidos associados à FAFIPA **ou** à Prefeitura de Curitiba quando PCI está selecionado. Desmarque o foco para ampliar a pesquisa. No filtro anual do PCI, ano `0` significa sem limite.
2. Atualize a tabela durante ou depois da coleta. Selecione os registros e clique em **Baixar selecionados**, ou baixe todos os resultados filtrados. O painel junto aos botões mostra a etapa, o arquivo atual e o progresso, atualizado a cada 3 segundos. Ele separa PDFs novos, reaproveitados, pendências de importação manual e falhas. Catalogar não baixa os PDFs.
3. **PCI com verificação:** abra a origem, conclua a verificação pessoalmente no navegador e use **Retomar após verificação**. A coleta tentará ler os links liberados e baixar os documentos. O coletor não compartilha os cookies do navegador; se o PCI continuar bloqueando, baixe o arquivo pelo site e envie-o em **Importar arquivos**.
4. **Meu acervo:** escolha entre **Disponíveis no acervo** e **Catalogados e pendentes**. Os documentos aparecem agrupados por concurso, com seus cargos e indicação do que já existe em PDF íntegro. A pesquisa textual exige todos os termos e ignora acentos; o CSV exporta exatamente a visão selecionada e seus filtros.
5. **Importar arquivos:** também aceita novos concursos e vários PDFs por envio. Escolha um tipo por importação; provas e gabaritos devem ser importados separadamente quando os tipos diferirem. É possível vincular o mesmo documento a vários cargos.

Campos vazios não restringem a busca. Separe termos por `;`: dentro de um campo vale **OU**; entre campos vale **E**. Cargo, banca, órgão e município usam correspondência parcial sem acentos; UF usa igualdade. Tipos filtram documentos já identificados, por isso registros aguardando verificação podem não aparecer quando um tipo estiver selecionado. Município e ano podem ser inferidos do órgão/edital; anos desconhecidos ficam fora de filtros anuais ativos.

Os filtros se adaptam às fontes. No Qconcursos, **Atualizar filtros Qconcursos** carrega as opções de banca, órgão, cargo, ano de aplicação, escolaridade, área de atuação e área de formação. Os anos podem ser selecionados individualmente. No PCI, os anos continuam disponíveis como intervalo e os campos de localidade ficam disponíveis. **Atualizar cargos PCI** acrescenta opções do site. Sem cargos selecionados, todos os cargos das fontes escolhidas serão pesquisados; isso pode levar bastante tempo. As páginas são percorridas integralmente, mesmo quando os anos não estão ordenados.

A coleta roda em segundo plano e aceita uma execução por vez. Fechar a aba não cancela o processo. O botão de cancelamento interrompe entre requisições e durante esperas/downloads; uma requisição em andamento pode levar até 35 segundos. Resultados já salvos são preservados. Ao reiniciar o serviço, execuções ativas são marcadas como interrompidas. **Executar novamente** repete a descoberta e reaproveita o conteúdo válido já salvo.

O acompanhamento é persistido no banco e reaparece ao recarregar a página. A barra mede arquivos processados (incluindo bloqueios e falhas), não bytes transferidos; durante a consulta às fontes, o indicador permanece animado sem porcentagem. O resumo final fica visível mesmo se a execução terminar rapidamente.

Quando há gabarito definitivo para o mesmo cargo/concurso e a mesma etapa/versão identificada, o preliminar é ocultado da listagem, pesquisa textual e CSV, e não entra na fila de downloads. A preferência vale mesmo se o definitivo ainda não foi baixado e antes dos filtros de tipo. Correspondências ambíguas preservam o preliminar. PDFs preliminares já salvos e sua procedência permanecem no disco e no banco.

## Fontes e limites

- PCI: catálogo público e reconhecimento de links diretos ou páginas com verificação. O layout atual libera URLs após verificação; a aplicação não resolve CAPTCHA nem contorna a proteção. Depois da confirmação humana, **Retomar após verificação** faz uma nova tentativa na sessão própria do coletor. Os cookies do navegador não são compartilhados; se o PCI continuar bloqueando, importe o PDF baixado no navegador.
- Qconcursos: índice público de provas e links diretos para prova, gabarito, alterações e edital. Escolaridade, área de atuação e área de formação podem ser usadas na busca. Os arquivos encontrados são validados pelo mesmo fluxo de PDF das outras fontes.
- A descoberta de páginas respeita o `robots.txt`. PDFs públicos já catalogados são tentados como downloads solicitados, com validação de HTTPS, domínio, redirecionamentos e conteúdo. CAPTCHA/login ou recusa persistente exigem intervenção; falhas temporárias, links removidos e erros de processamento ficam separados. Em **Catalogados e pendentes**, é possível tentar novamente os downloads que ainda podem ser automatizados.
- Os domínios de anexos aceitos são explícitos no cliente HTTP. Redirecionamentos também passam por validação e regras de acesso do domínio de destino. Há limite mínimo de 2,5 segundos por domínio e até três tentativas para falhas transitórias.
- O Qconcursos informa o ano da prova no título do registro. Um gabarito pode conter vários cargos; o vínculo é ao documento inteiro, sem extrair respostas individuais.
- Cargo parecido é sugestão de estudo. O aplicativo não afirma equivalência com o edital, não separa questões e não resolve verificações, CAPTCHA ou login.
- PDFs digitalizados sem camada textual permanecem no acervo como **sem texto pesquisável**. OCR está fora desta versão. Extração protegida ou malsucedida aparece separadamente.
- Limite por PDF: 200 MB. A visualização embutida fica disponível até 20 MB; arquivos maiores podem ser baixados normalmente.

## Dados e backup

Tudo fica em `acervo_pci/`, montada no contêiner em `/data`:

- `provas.sqlite3`: catálogo, vínculos, texto pesquisável e execuções;
- `pdfs/`: documentos organizados pelo hash SHA-256, sem duplicar conteúdo;
- `catalogo.csv`: exportação acumulada, atualizada ao fim das execuções/importações;
- `backups/`: cópia SQLite anterior à migração do catálogo legado;
- `logs/`: saída dos subprocessos de coleta.

A migração inicial mantém as tabelas antigas e copia seus registros para o novo modelo, após backup SQLite consistente. O arquivo `Edital_base.pdf` é opcional e não acompanha o repositório. Para usá-lo, disponibilize-o localmente e configure `EDITAL_PATH` com um caminho montado no contêiner.

A atualização para a versão 3 cria um backup `backups/antes-migracao-v3-*.sqlite3`, preserva arquivos e histórico, grava edital e metadados estruturados das tentativas e libera para nova tentativa os casos antigos bloqueados exclusivamente por `robots.txt`. A versão 4 cria backup antes de remover concursos, registros e origens da FAFIPA, remove PDFs sem outros vínculos e mantém os documentos compartilhados. Ela também acrescenta metadados de filtros do Qconcursos. A migração não inicia downloads.

Para fazer uma cópia completa e consistente, pare o serviço e copie **toda** a pasta `acervo_pci` para outro local. Inclua os arquivos SQLite auxiliares, se existirem. Depois, inicie novamente:

```powershell
docker compose stop
# Copie acervo_pci para o local de backup desejado.
docker compose start
```

Para restaurar, mantenha o serviço parado e restaure a pasta inteira. Recriar o contêiner com `docker compose up --build -d` mantém os dados montados.

## Linha de comando

A CLI e a interface usam o mesmo coletor e banco:

```powershell
docker compose exec acervo python coletor_provas_pci.py --help
docker compose exec acervo python coletor_provas_pci.py --fonte QCONCURSOS --cargo "Analista Administrativo" --ano-inicio 2016 --download
docker compose exec acervo python coletor_provas_pci.py --fonte QCONCURSOS --cargo "Analista Administrativo" --ano 2021 --ano 2022
docker compose exec acervo python coletor_provas_pci.py --fonte PCI --correlatas --foco
docker compose exec acervo python coletor_provas_pci.py --somente-download --todos-cargos
```

Sem Docker: Python 3.12+, `pip install -r requirements.txt`, depois `python entrypoint.py`. Para usar apenas a CLI, execute `python coletor_provas_pci.py`; a saída padrão é `acervo_pci`. `--saida` ou a variável `ACERVO_DIR` alteram a pasta. `EDITAL_PATH` altera a localização do edital exibido na interface.

## Verificação

```powershell
docker compose exec acervo python -m pytest -q
```

Os testes usam bancos temporários e PDFs de teste, sem alterar o acervo pessoal nem depender da disponibilidade dos sites. Cobrem migração, filtros, parsers, classificação, importação, deduplicação, pesquisa textual, exclusão de execuções simultâneas, cancelamento e inicialização da interface.
