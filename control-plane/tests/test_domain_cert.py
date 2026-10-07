"""Собственный TLS-сертификат домена приложения: проверка пары ключ/сертификат, Secret в кластере, привязка к Ingress."""
import base64
import datetime as dt
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID
from kubernetes.client.exceptions import ApiException

from app.db.base import SessionLocal
from app.db.models import AuditEvent
from app.services import certs, kubeclient

NOW = dt.datetime.now(dt.timezone.utc)
HOST = "app.example.com"


def _ec():
    return ec.generate_private_key(ec.SECP256R1())


def _pem_cert(c):
    return c.public_bytes(serialization.Encoding.PEM).decode()


def _pem_key(k, enc=None):
    return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc or serialization.NoEncryption()).decode()


def _ca(cn="Test CA"):
    k = _ec()
    n = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    c = (x509.CertificateBuilder().subject_name(n).issuer_name(n).public_key(k.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(NOW - dt.timedelta(days=1)).not_valid_after(NOW + dt.timedelta(days=3650))
         .add_extension(x509.BasicConstraints(ca=True, path_length=None), True).sign(k, hashes.SHA256()))
    return k, c


def _leaf(ca, names=(HOST,), start=-1, end=365, key=None, ca_flag=False, self_sign=False):
    key = key or _ec()
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]) if self_sign else ca[1].subject
    signer = key if self_sign else ca[0]
    c = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])])).issuer_name(issuer)
         .public_key(key.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(NOW + dt.timedelta(days=start)).not_valid_after(NOW + dt.timedelta(days=end))
         .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), False)
         .add_extension(x509.BasicConstraints(ca=ca_flag, path_length=None), True).sign(signer, hashes.SHA256()))
    return key, c


CA = _ca()


def _code(fn, *a):
    with pytest.raises(certs.CertError) as e:
        fn(*a)
    return e.value.code


def test_valid_chain_and_key_pass_and_are_normalized():
    k, c = _leaf(CA)
    info, chain, key = certs.validate(_pem_cert(c) + _pem_cert(CA[1]), _pem_key(k), HOST)
    assert info["host_match"] and info["days_left"] >= 364 and info["warnings"] is None
    assert chain.count("BEGIN CERTIFICATE") == 2 and "BEGIN PRIVATE KEY" in key


def test_wildcard_certificate_covers_subdomain():
    k, c = _leaf(CA, names=("*.example.com",))
    info, _, _ = certs.validate(_pem_cert(c) + _pem_cert(CA[1]), _pem_key(k), HOST)
    assert info["host_match"]
    assert _code(certs.validate, _pem_cert(c), _pem_key(k), "a.b.example.com") == "cert_hostname_mismatch"      # wildcard — один уровень


def test_rejects_mismatched_key_expired_wrong_name_encrypted_and_weak():
    k, c = _leaf(CA)
    assert _code(certs.validate, _pem_cert(c), _pem_key(_ec()), HOST) == "key_mismatch"
    k2, c2 = _leaf(CA, start=-400, end=-35)
    assert _code(certs.validate, _pem_cert(c2), _pem_key(k2), HOST) == "cert_expired"
    k3, c3 = _leaf(CA, start=5, end=100)
    assert _code(certs.validate, _pem_cert(c3), _pem_key(k3), HOST) == "cert_not_yet_valid"
    assert _code(certs.validate, _pem_cert(c), _pem_key(k), "other.example.org") == "cert_hostname_mismatch"
    enc = _pem_key(k, serialization.BestAvailableEncryption(b"pw"))
    assert _code(certs.validate, _pem_cert(c), enc, HOST) == "key_encrypted"
    weak = rsa.generate_private_key(65537, 1024)
    kw, cw = _leaf(CA, key=weak)
    assert _code(certs.validate, _pem_cert(cw), _pem_key(weak), HOST) == "key_weak"
    assert _code(certs.validate, "not a pem", _pem_key(k), HOST) == "cert_invalid"
    assert _code(certs.validate, _pem_cert(c), "junk", HOST) == "key_invalid"
    assert _code(certs.validate, _pem_cert(CA[1]) + _pem_cert(c), _pem_key(k), HOST) == "leaf_is_ca"      # CA первым — перепутан порядок


def test_chain_warnings():
    k, c = _leaf(CA)
    assert certs.validate(_pem_cert(c), _pem_key(k), HOST)[0]["warnings"] == ["chain_incomplete"]
    other = _ca("Other CA")
    assert "chain_order" in certs.validate(_pem_cert(c) + _pem_cert(other[1]), _pem_key(k), HOST)[0]["warnings"]
    ks, cs = _leaf(CA, self_sign=True)
    assert "self_signed" in certs.validate(_pem_cert(cs), _pem_key(ks), HOST)[0]["warnings"]
    k5, c5 = _leaf(CA, start=-300, end=9)
    assert "cert_expires_soon" in certs.validate(_pem_cert(c5) + _pem_cert(CA[1]), _pem_key(k5), HOST)[0]["warnings"]


# ---------- API с фейковым кластером ----------

def api_error(status):
    return ApiException(status=status, reason=str(status))


class Net:
    def __init__(self):
        self.items = {}

    def _store(self, ns, doc, rv):
        spec = doc["spec"]
        tls = [SimpleNamespace(hosts=t.get("hosts"), secret_name=t.get("secretName")) for t in spec.get("tls", [])]
        self.items[(ns, doc["metadata"]["name"])] = SimpleNamespace(doc=doc, metadata=SimpleNamespace(resource_version=rv, labels=doc["metadata"].get("labels")),
                                                                     spec=SimpleNamespace(rules=[SimpleNamespace(host=spec["rules"][0]["host"])], tls=tls))

    def read_namespaced_ingress(self, name, ns):
        if (ns, name) not in self.items:
            raise api_error(404)
        return self.items[(ns, name)]

    def create_namespaced_ingress(self, ns, doc):
        self._store(ns, doc, "1")

    def replace_namespaced_ingress(self, name, ns, doc):
        self._store(ns, doc, "2")

    def delete_namespaced_ingress(self, name, ns):
        del self.items[(ns, name)]


class Core:
    def __init__(self):
        self.secrets = {}

    def read_namespaced_service(self, name, ns):
        return SimpleNamespace(spec=SimpleNamespace(ports=[SimpleNamespace(name="http", port=8080)]))

    def read_namespaced_secret(self, name, ns):
        if (ns, name) not in self.secrets:
            raise api_error(404)
        return self.secrets[(ns, name)]

    def create_namespaced_secret(self, ns, body):
        if (ns, body.metadata.name) in self.secrets:
            raise api_error(409)
        self.secrets[(ns, body.metadata.name)] = body

    def replace_namespaced_secret(self, name, ns, body):
        self.secrets[(ns, name)] = body

    def delete_namespaced_secret(self, name, ns):
        del self.secrets[(ns, name)]


@pytest.fixture
def cluster(monkeypatch):
    net, core = Net(), Core()
    monkeypatch.setattr(kubeclient, "networking_v1_api", lambda cluster=None: net)
    monkeypatch.setattr(kubeclient, "core_v1_api", lambda cluster=None: core)
    return net, core


BODY = {"slug": "shop", "repo_full_name": "acme/shop", "environments": [
    {"name": "prod", "namespace": "shop-prod", "deployment_name": "shop", "container_name": "shop"}]}
URL = "/api/projects/shop/environments/prod"


def _bundle(names=(HOST,), **kw):
    k, c = _leaf(CA, names=names, **kw)
    return {"certificate": _pem_cert(c) + _pem_cert(CA[1]), "private_key": _pem_key(k)}


def _setup(client, admin):
    client.post("/api/projects", json=BODY, headers=admin)
    assert client.put(f"{URL}/domain", json={"host": HOST}, headers=admin).status_code == 200


def test_upload_binds_the_certificate_to_the_ingress(client, admin, cluster):
    net, core = cluster
    _setup(client, admin)
    assert client.get(f"{URL}/domain/certificate", headers=admin).json()["source"] == "none"
    b = _bundle()
    r = client.put(f"{URL}/domain/certificate", json=b, headers=admin)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["source"] == "custom" and out["secret"] == "shop-tls" and out["result"] == "created" and out["host_match"]
    assert "private_key" not in r.text and "PRIVATE KEY" not in r.text
    sec = core.secrets[("shop-prod", "shop-tls")]
    assert sec.type == "kubernetes.io/tls" and base64.b64decode(sec.data["tls.key"]).decode().startswith("-----BEGIN PRIVATE KEY")
    ing = net.items[("shop-prod", "shop")].doc
    assert ing["spec"]["tls"] == [{"hosts": [HOST], "secretName": "shop-tls"}]
    ann = ing["metadata"].get("annotations", {})
    assert ann.get("traefik.ingress.kubernetes.io/router.tls") == "true" and "traefik.ingress.kubernetes.io/router.tls.certresolver" not in ann
    got = client.get(f"{URL}/domain/certificate", headers=admin).json()
    assert got["source"] == "custom" and got["days_left"] >= 364 and "PRIVATE" not in str(got)
    db = SessionLocal()
    ev = db.query(AuditEvent).filter_by(action="domain_certificate_set").one()
    assert "PRIVATE" not in repr(ev.detail) and ev.detail["host"] == HOST and len(ev.detail["sha256"]) == 64
    db.close()
    # замена сертификата (продление): тот же Secret обновляется
    assert client.put(f"{URL}/domain/certificate", json=_bundle(), headers=admin).json()["result"] == "updated"


def test_same_domain_keeps_certificate_but_new_domain_resets_it(client, admin, cluster):
    net, core = cluster
    _setup(client, admin)
    client.put(f"{URL}/domain/certificate", json=_bundle(), headers=admin)
    r = client.put(f"{URL}/domain", json={"host": HOST}, headers=admin).json()
    assert "custom_certificate_reset" not in r and net.items[("shop-prod", "shop")].doc["spec"]["tls"][0]["secretName"] == "shop-tls"
    r = client.put(f"{URL}/domain", json={"host": "new.example.com"}, headers=admin).json()
    assert r["custom_certificate_reset"] is True and ("shop-prod", "shop-tls") not in core.secrets        # старый ключ удалён из кластера
    assert "secretName" not in str(net.items[("shop-prod", "shop")].doc["spec"].get("tls"))


def test_delete_returns_the_platform_certificate_and_removes_the_key(client, admin, cluster):
    net, core = cluster
    _setup(client, admin)
    client.put(f"{URL}/domain/certificate", json=_bundle(), headers=admin)
    assert client.delete(f"{URL}/domain/certificate", headers=admin).json()["result"] == "removed"
    assert ("shop-prod", "shop-tls") not in core.secrets and "secretName" not in str(net.items[("shop-prod", "shop")].doc["spec"].get("tls"))
    assert client.delete(f"{URL}/domain/certificate", headers=admin).json()["result"] == "none"


def test_errors_permissions_and_foreign_secrets(client, admin, make_token, cluster):
    net, core = cluster
    client.post("/api/projects", json=BODY, headers=admin)
    assert client.put(f"{URL}/domain/certificate", json=_bundle(), headers=admin).status_code == 409        # сначала домен
    client.put(f"{URL}/domain", json={"host": HOST}, headers=admin)
    assert client.put(f"{URL}/domain/certificate", json=_bundle(), headers=make_token("viewer")).status_code == 403
    assert client.put(f"{URL}/domain/certificate", json=_bundle(), headers=make_token("developer")).status_code == 403
    bad = client.put(f"{URL}/domain/certificate", json=_bundle(names=("other.example.org",)), headers=admin)
    assert bad.status_code == 422 and bad.json()["detail"]["error"] == "cert_hostname_mismatch"
    k, c = _leaf(CA)
    mism = {"certificate": _pem_cert(c), "private_key": _pem_key(_ec())}
    assert client.put(f"{URL}/domain/certificate", json=mism, headers=admin).json()["detail"]["error"] == "key_mismatch"
    # чужой Secret с таким именем не перезаписывается
    core.secrets[("shop-prod", "shop-tls")] = SimpleNamespace(metadata=SimpleNamespace(labels={"owner": "someone"}), data={"tls.crt": "eA=="}, type="kubernetes.io/tls")
    r = client.put(f"{URL}/domain/certificate", json=_bundle(), headers=admin)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "secret_not_managed"
    assert core.secrets[("shop-prod", "shop-tls")].data == {"tls.crt": "eA=="}
