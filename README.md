# 🔥 Vinted Alerta (v2)

Bot do Telegram com **barra de pesquisa da Vinted**. Pesquisas, segues artigos ou pesquisas inteiras, e recebes um alerta quando um artigo começa a ganhar muitos likes. Por omissão o alerta dispara com **5 likes em 1 hora**, e a regra edita-se no botão **✏️ Editar**.

Corre de graça no **GitHub Actions**, sem PC ligado.

## No telemóvel

- A caixa de texto do bot diz **«🔎 Pesquisar na Vinted…»**. Escreve, por exemplo, `casaco levis` e recebes 8 resultados com preço, ❤️ likes, tamanho e marca.
- Por baixo dos resultados há estes botões:
  - **🔔 1–8**: seguir esse artigo
  - **🆕 Recentes · ❤️ Likes · 💶 Preço**: reordenar
  - **📌 Seguir pesquisa**: avisa quando um artigo dessa pesquisa dispara em likes
  - **Abrir na Vinted ↗**
- Para seguir um artigo, também podes **colar o link** dele.
- **❤️ Favoritos** (opcional): com a sessão da Vinted configurada, segue também os artigos que marcas com ❤️.
- Teclado fixo: **📊 Estado · 📌 Seguidos · ⚙️ Regras · ❓ Ajuda**

**Alertas:** 🔥 *Está a aquecer!* (+N likes em X min), com foto e botão «Abrir na Vinted».
Para favoritos e artigos seguidos há ainda 💸 *Baixou de preço* e 🔒 *Reservado / vendido*. Os dois podem ser desligados.

**⚙️ Regras → ✏️ Editar**

| Botões | O que muda |
|---|---|
| ➖ ❤️ 5 likes ➕ | likes necessários (1–200) |
| ➖ 🕐 em 1 h ➕ | janela de tempo (10 min – 24 h) |
| ➖ ⏱️ repetir após 6 h ➕ | pausa antes de voltar a avisar o mesmo artigo |
| 💸 Preço ✅ / 🔒 Reservado ✅ | ligar/desligar esses alertas |

Limites: 5 pesquisas e 20 artigos seguidos (para não sobrecarregar a Vinted).

> ℹ️ O bot acorda de 5 em 5 min e fica ~4 min à escuta, por isso a resposta é quase sempre imediata. Quando o GitHub atrasa, pode levar alguns minutos.

---

## Instalação (≈ 10 min, uma vez, no PC)

### 1. Criar o bot do Telegram
No Telegram abre **@BotFather** → `/newbot` → escolhe um nome → guarda o **token** (`123456:ABC-…`).

### 2. Criar o repositório no GitHub
1. github.com → **New repository** → nome `vinted-alerta` → **Public** → Create.
   *(Público = Actions grátis e ilimitadas. Os teus dados ficam em Secrets e o estado vai encriptado.)*
2. **Add file → Upload files** → arrasta **o conteúdo** desta pasta → **Commit**.
   - Se a pasta `.github` não for carregada: **Add file → Create new file**, nome `.github/workflows/vinted-alerta.yml`, e cola o conteúdo.

### 3. Secrets
**Settings → Secrets and variables → Actions → New repository secret**

| Nome | Valor |
|---|---|
| `TELEGRAM_TOKEN` | token do passo 1 |
| `STATE_KEY` | uma frase secreta longa à tua escolha |

Opcionais:
- `VINTED_COOKIE` + `VINTED_USER_ID`: só para seguir os ❤️ favoritos (ver abaixo)
- `TELEGRAM_CHAT_ID`: senão o bot fica associado ao primeiro `/start`
- `PROXY_URL`: ver «Bloqueios»
- Para outro país, cria a *variable* `VINTED_DOMAIN` (ex.: `www.vinted.es`)

### 4. Ligar
1. **Actions** → se pedir, ativa → **Vinted Alerta** → **Run workflow**.
2. No Telegram abre o teu bot → **/start**.
3. Escreve uma pesquisa na caixa. Se vierem resultados, está tudo a funcionar. ✅

---

## Seguir os favoritos (opcional)
1. No Chrome do PC, entra em vinted.pt com a tua conta.
2. **F12** → **Rede/Network** → recarrega → filtra `api/v2` → clica num pedido → em **Request Headers** copia o valor de **`cookie`**.
3. O teu ID é o número do link do perfil: `vinted.pt/member/`**`123456789`**`-nome`.
4. Guarda-os nos secrets `VINTED_COOKIE` e `VINTED_USER_ID`, ou envia ao bot `/cookie <valor>` (a mensagem é apagada logo a seguir).

Se a sessão expirar, o bot avisa. Pesquisas e artigos seguidos **não precisam de sessão** e continuam a funcionar.

## Bloqueios
A Vinted tem proteção anti-bot (DataDome) e pode bloquear pedidos vindos dos servidores do GitHub. O bot avisa «🚧 A Vinted bloqueou» e volta a tentar sozinho. Se for constante:

- **Correr em casa** (PC ou Raspberry Pi), com a tua internet. Assim as respostas também são instantâneas:
  ```bash
  pip install -r requirements.txt
  export TELEGRAM_TOKEN=... STATE_KEY='...'      # + VINTED_COOKIE / VINTED_USER_ID se quiseres favoritos
  python -m vinted_alerta --loop
  ```
- **Proxy residencial** no secret `PROXY_URL` (`http://user:pass@host:porta`)

## Avisos honestos
- **Não há API oficial.** Em outubro de 2026 a Vinted desativou `/api/v2/catalog/items`, e esta versão já lê o formato novo, com os dados embutidos nas páginas. Se a Vinted voltar a mudar, a pesquisa avisa «formato mudou?» e é preciso ajustar o código.
- **Termos de uso.** O acesso automatizado vai contra os termos da Vinted. A leitura de 10 em 10 min, só do que segues, é discreta, mas o risco não é zero.
- **Como funciona a deteção.** Compara os likes atuais com o mínimo dentro da janela. Com leituras espaçadas, aceita uma amostra até 25 % antes da janela (ex.: 75 min para 1 h).

## Estrutura
```
.github/workflows/vinted-alerta.yml   agendamento + estado em cache
vinted_alerta/vinted.py               leitura da Vinted (pesquisa, artigo, favoritos)
vinted_alerta/detector.py             regra «N likes em X min», preço, reservado
vinted_alerta/bot.py                  barra de pesquisa, seguir, painel ✏️ Editar
vinted_alerta/main.py                 ciclo principal
tests/                                testes (python -m pytest)
```
