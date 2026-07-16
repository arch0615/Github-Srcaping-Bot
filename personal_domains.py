"""Free / consumer email providers => treated as 'personal'.

Anything NOT in this set is treated as a business/company/custom domain and
skipped.  Extend the set freely; matching is case-insensitive on the full
domain after the '@'.
"""

PERSONAL_EMAIL_DOMAINS = {
    # Google
    "gmail.com", "googlemail.com",
    # Microsoft
    "outlook.com", "hotmail.com", "live.com", "msn.com", "outlook.de",
    "hotmail.co.uk", "hotmail.fr", "live.co.uk", "live.com.au",
    # Yahoo / Oath
    "yahoo.com", "yahoo.co.uk", "yahoo.fr", "yahoo.de", "yahoo.co.in",
    "ymail.com", "rocketmail.com",
    # Apple
    "icloud.com", "me.com", "mac.com",
    # Privacy-focused
    "proton.me", "protonmail.com", "pm.me", "tutanota.com", "tutanota.de",
    "tuta.io", "mailbox.org", "posteo.de", "fastmail.com", "fastmail.fm",
    "hey.com", "duck.com",
    # Other global consumer providers
    "aol.com", "gmx.com", "gmx.de", "gmx.net", "web.de", "zoho.com",
    "yandex.com", "yandex.ru", "mail.ru", "mail.com", "inbox.com",
    "hushmail.com", "tutamail.com",
    # Regional consumer providers
    "qq.com", "163.com", "126.com", "sina.com", "foxmail.com",      # CN
    "naver.com", "daum.net", "hanmail.net",                          # KR
    "rediffmail.com",                                                # IN
    "libero.it", "virgilio.it", "tin.it",                           # IT
    "orange.fr", "free.fr", "laposte.net", "wanadoo.fr", "sfr.fr",  # FR
    "t-online.de", "freenet.de",                                    # DE
    "bol.com.br", "uol.com.br", "terra.com.br",                     # BR
    "seznam.cz",                                                    # CZ
    "wp.pl", "o2.pl", "interia.pl", "onet.pl",                      # PL
    "bigpond.com", "optusnet.com.au",                              # AU
    "telus.net", "shaw.ca", "sympatico.ca",                        # CA
}


def is_personal_email(email: str) -> bool:
    """True if the email's domain is a known consumer/free provider."""
    if not email or "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1].strip().lower()
    return domain in PERSONAL_EMAIL_DOMAINS


def domain_of(email: str) -> str:
    return email.rsplit("@", 1)[1].strip().lower() if email and "@" in email else ""
