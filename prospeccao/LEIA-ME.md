# Prospecção Nort (bastidores)

Rotina automática que alimenta o site Prospecção Nort.

- `robo_cnpj_nort.py`: baixa os dados abertos do CNPJ (Receita, mensal) e da dívida ativa (PGFN, trimestral) e monta as bases.
- `.github/workflows/prospeccao.yml`: roda todo dia à meia-noite; quando sai base nova, publica uma release com os dados criptografados.
- Os dados saem criptografados (chave pública em `chave_publica.pem`). Só a rotina de entrega do site tem a chave para abrir.

Não é preciso mexer aqui no dia a dia.
