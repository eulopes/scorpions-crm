import requests
import re
import time
import urllib.parse
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class CSRFScanner:
    def __init__(self, url):
        self.url = url.rstrip('/')
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "*/*",
            "Connection": "close"
        })
        
    def test_csrf_protection(self):
        """Verifica se os endpoints têm proteção CSRF"""
        endpoints = ["/admin", "/profile", "/settings", "/account"]
        
        for endpoint in endpoints:
            try:
                response = self.session.get(f"{self.url}{endpoint}")
                # Procura por tokens CSRF em formulários
                csrf_tokens = re.findall(r'<input[^>]*name=["\'](_token|csrf_token)["\'][^>]*value=["\']([^"\']*)["\'][^>]*>', response.text)
                
                if not csrf_tokens:
                    logger.warning(f"[!] Endpoint {endpoint} sem token CSRF!")
                else:
                    logger.info(f"[+] Endpoint {endpoint} protegido por token CSRF")
                    
            except Exception as e:
                logger.error(f"Erro testando CSRF em {endpoint}: {e}")
                
    def validate_input(self):
        """Valida se os endpoints tratam entradas corretamente"""
        test_cases = [
            {"field": "username", "payload": "admin'--"},
            {"field": "email", "payload": "test@example.com' OR '1'='1"},
            {"field": "password", "payload": "password' OR '1'='1"}
        ]
        
        endpoints = ["/login", "/register", "/reset-password"]
        
        for endpoint in endpoints:
            for case in test_cases:
                try:
                    response = self.session.get(f"{self.url}{endpoint}")
                    inputs = re.findall(r'<input[^>]*name=["\']([^"\']*)["\'][^>]*>', response.text)
                    
                    if case["field"] in inputs:
                        data = {case["field"]: case["payload"]}
                        response = self.session.post(f"{self.url}{endpoint}", data=data)
                        
                        if "error" in response.text.lower() or "syntax" in response.text.lower():
                            logger.warning(f"[!] Input {case['field']} em {endpoint} não tratado corretamente!")
                            
                except Exception as e:
                    logger.error(f"Erro validando entrada em {endpoint}: {e}")

# Executar testes
scanner = CSRFScanner("https://scorpions-crm-production.up.railway.app/")
scanner.test_csrf_protection()
scanner.validate_input()