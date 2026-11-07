"""Create local test certificates without changing any OS trust store."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
import ipaddress,socket
from cryptography import x509
from cryptography.x509.oid import NameOID,ExtendedKeyUsageOID
from cryptography.hazmat.primitives import serialization,hashes
from cryptography.hazmat.primitives.asymmetric import rsa

root=Path(__file__).resolve().parents[1]/'data'/'tls'; root.mkdir(parents=True,exist_ok=True)
if (root/'local-ca-key.pem').exists(): raise SystemExit('Existing certificates preserved. Reuse them rather than replacing the trusted CA.')
now=datetime.now(timezone.utc); ca_key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'MinePath Local Test CA')])
ca=x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(ca_key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=365)).add_extension(x509.BasicConstraints(ca=True,path_length=0),True).add_extension(x509.KeyUsage(False,False,False,False,False,True,True,False,False),True).sign(ca_key,hashes.SHA256())
key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
hosts={'localhost',socket.gethostname()}; ips={'127.0.0.1'}
try: ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
except OSError: pass
sans=[x509.DNSName(host) for host in hosts]+[x509.IPAddress(ipaddress.ip_address(ip)) for ip in ips]
cert=x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'MinePath Local Server')])).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=90)).add_extension(x509.SubjectAlternativeName(sans),False).add_extension(x509.BasicConstraints(ca=False,path_length=None),True).add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),False).sign(ca_key,hashes.SHA256())
for filename,value in [('local-ca.pem',ca.public_bytes(serialization.Encoding.PEM)),('server.pem',cert.public_bytes(serialization.Encoding.PEM)),('local-ca-key.pem',ca_key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())),('server-key.pem',key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))]:
    (root/filename).write_bytes(value)
print('Certificates created:',root)
print('Trust only local-ca.pem on the test device. Never share either private key. No OS settings were modified.')
print('Available names/IPs:',', '.join(sorted(hosts|ips)))
