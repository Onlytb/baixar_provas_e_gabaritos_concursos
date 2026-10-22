# Acervo de provas e gabaritos de concursos

Aplicação local para pesquisar, catalogar, baixar e organizar provas e gabaritos públicos de concursos. A interface abre no navegador do próprio computador. A coleta respeita as regras dos sites: verificações, CAPTCHA e login precisam ser concluídos pela pessoa usuária.

## O que você precisa

A forma mais simples de executar é com **Docker Desktop**, disponível para Windows, macOS e Linux. No Windows, instale o Docker Desktop e abra-o; espere até indicar que o Docker está em execução. Você também precisará do Git para copiar o projeto. Se preferir não instalar Docker, há uma alternativa com Python mais abaixo.

## Instalação no Windows

1. Instale o [Git para Windows](https://git-scm.com/download/win) e o [Docker Desktop](https://www.docker.com/products/docker-desktop/). Durante a instalação do Docker, aceite o uso do WSL 2 se essa opção for apresentada. Reinicie o computador se o instalador pedir.
2. Abra o Docker Desktop e aguarde ficar pronto.
3. Abra o **PowerShell**. Você pode encontrá-lo no menu Iniciar.
4. Copie e cole estes comandos, um por vez:

```powershell
git clone https://github.com/Onlytb/baixar_provas_e_gabaritos_concursos.git
cd baixar_provas_e_gabaritos_concursos
docker compose up --build -d
```

Na primeira execução, o Docker baixa os componentes necessários e monta a aplicação; isso pode demorar alguns minutos. Quando terminar, abra [http://localhost:8501](http://localhost:8501) no navegador. A aplicação fica disponível apenas no seu próprio computador.

> Para iniciar uma cópia já baixada, abra o PowerShell na pasta `baixar_provas_e_gabaritos_concursos` e rode `docker compose up -d`.

## Usar a aplicação

1. **Pesquisar:** ajuste os filtros à esquerda e clique em **Pesquisar e catalogar**. A pesquisa inicial se concentra nos cargos escolhidos junto à FAFIPA ou à Prefeitura de Curitiba. Desmarque o foco para ampliar os resultados. Ano `0` significa sem limite.
2. **Baixar:** catalogar não baixa os PDFs. Atualize a tabela, selecione os registros e clique em **Baixar selecionados**, ou peça o download de todos os resultados filtrados. O painel mostra o andamento e separa PDFs baixados, reaproveitados, pendências e falhas. É possível fechar a aba enquanto a execução continua.
3. **Resolver verificações do PCI:** abra a página da origem, conclua a verificação no navegador e baixe o PDF. Depois, em **Importar arquivos**, selecione o cargo e envie o arquivo.
4. **Consultar o acervo:** use **Meu acervo** para ver documentos disponíveis ou catalogados que ainda estão pendentes. Os documentos são organizados por concurso; a pesquisa textual ignora acentos e exige que todos os termos informados apareçam.
5. **Importar PDFs próprios:** em **Importar arquivos**, envie um ou mais documentos. Escolha um tipo por envio; provas e gabaritos de tipos diferentes devem ser enviados separadamente. Um mesmo documento pode ser associado a vários cargos.

Campos de texto vazios não limitam a busca. Separe opções com ponto e vírgula: opções no mesmo campo funcionam como **OU** e campos diferentes como **E**. Cargo, banca, órgão e município aceitam correspondência parcial e ignoram acentos; UF exige igualdade. Sem cargos selecionados, a pesquisa pode abranger todos os cargos da fonte e demorar bastante.

A coleta acontece em segundo plano e permite uma execução por vez. O botão de cancelamento interrompe entre requisições; uma requisição já iniciada pode levar até 35 segundos. Ao reiniciar a aplicação, uma execução que ainda constava ativa será marcada como interrompida. Os dados já salvos são preservados.

## Parar e iniciar novamente

No PowerShell, dentro da pasta do projeto:

```powershell
# Parar sem apagar os dados
docker compose stop

# Iniciar novamente
docker compose start

# Ver se o serviço está ativo
docker compose ps

# Ver mensagens recentes para investigar um problema
docker compose logs --tail 80 acervo
```

Para encerrar e remover o contêiner, preservando os dados, use `docker compose down`. Para voltar a executar, use `docker compose up -d`.

## Onde ficam os dados

A pasta `acervo_pci/` é criada localmente quando a aplicação inicia. Ela guarda o banco de dados, PDFs, exportação CSV, cópias de segurança e registros técnicos. **Essa pasta e os arquivos baixados não fazem parte do repositório Git**; cada pessoa mantém o próprio acervo no computador. Não apague essa pasta se quiser preservar seus dados.

Para fazer backup, pare a aplicação com `docker compose stop` e copie a pasta `acervo_pci` para outro local. Depois, inicie de novo com `docker compose start`. Para restaurar, pare o serviço e substitua a pasta pela cópia de backup.

O arquivo `Edital_base.pdf` é opcional e não acompanha o projeto. Ele serve apenas como material de referência na interface; sem ele, o restante da aplicação funciona normalmente.

## Alternativa: executar com Python

Use Python **3.12 ou mais recente**. No PowerShell, dentro da pasta do projeto, crie um ambiente isolado e instale as bibliotecas:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python entrypoint.py
```

Se o PowerShell bloquear a ativação do ambiente, abra o Prompt de Comando e use:

```bat
.venv\Scripts\activate.bat
```

Com a aplicação iniciada, abra [http://localhost:8501](http://localhost:8501). Para interromper, volte ao terminal e pressione `Ctrl+C`. A alternativa Python também cria e usa `acervo_pci/` na pasta do projeto.

## Linha de comando (opcional)

Quem preferir pode executar a coleta sem abrir a interface. Com Docker, os exemplos abaixo rodam dentro do contêiner:

```powershell
docker compose exec acervo python coletor_provas_pci.py --help
docker compose exec acervo python coletor_provas_pci.py --fonte FAFIPA --cargo "Analista Administrativo" --ano-inicio 2016 --download
docker compose exec acervo python coletor_provas_pci.py --fonte PCI --correlatas --foco
docker compose exec acervo python coletor_provas_pci.py --somente-download --todos-cargos
```

Com Python instalado diretamente, remova o trecho `docker compose exec acervo` do início dos comandos. A CLI e a interface usam o mesmo acervo local.

## Fontes e limites

- **PCI:** catálogo público. Algumas páginas liberam o arquivo somente depois de uma verificação feita pela pessoa usuária.
- **FAFIPA:** índices de concursos, cargos e documentos públicos disponíveis no portal. Nem todo concurso publica provas.
- A aplicação não resolve CAPTCHA ou login, não extrai respostas individuais de gabaritos e não afirma que cargos parecidos têm conteúdo equivalente.
- PDF digitalizado sem texto pesquisável continua disponível para download, mas não pode ser pesquisado por conteúdo. OCR não está incluído.
- Arquivos de até 200 MB podem ser importados. A visualização dentro da aplicação está disponível até 20 MB.
- Os downloads são feitos sob solicitação da pessoa usuária, com limite de acesso por domínio e validação de endereço e conteúdo.

## Problemas comuns

- **“docker” não é reconhecido:** instale/abra o Docker Desktop e abra uma nova janela do PowerShell.
- **Docker não iniciou:** abra o Docker Desktop, aguarde ficar pronto e tente novamente.
- **A página não abre:** confirme que `docker compose ps` mostra o serviço ativo e consulte `docker compose logs --tail 80 acervo`.
- **A porta 8501 já está em uso:** feche o outro programa que a utiliza e rode `docker compose up -d` novamente.
- **Quero recomeçar sem perder o acervo:** use `docker compose down` e depois `docker compose up -d`. Não apague a pasta `acervo_pci`.
