"""Gera um hash bcrypt de senha para colar em .streamlit/secrets.toml, em [AUTH_USERS].

Uso:
    python scripts/gerar_hash_senha.py
"""

from getpass import getpass

import bcrypt


def main() -> None:
    senha = getpass("Digite a nova senha: ")
    confirmacao = getpass("Confirme a nova senha: ")
    if senha != confirmacao:
        print("As senhas nao coincidem.")
        return
    hash_bcrypt = bcrypt.hashpw(senha.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    print("\nAdicione esta linha em .streamlit/secrets.toml, dentro de [AUTH_USERS]:")
    print(f'seu_usuario = "{hash_bcrypt}"')


if __name__ == "__main__":
    main()
