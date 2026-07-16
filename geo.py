"""Best-effort mapping of a free-text GitHub `location` string to a continent.

Location on GitHub is unstructured ("Berlin", "SF, CA, USA", "India", "中国"),
so this is heuristic: we scan the lowercased text for known country names /
codes and a set of major cities. Returns a continent name or "Unknown".
"""

# country (and common alias) -> continent
_COUNTRIES = {
    "Africa": [
        "algeria", "angola", "benin", "botswana", "burkina faso", "burundi",
        "cameroon", "cape verde", "chad", "congo", "drc", "djibouti", "egypt",
        "ethiopia", "gabon", "gambia", "ghana", "guinea", "ivory coast",
        "côte d'ivoire", "kenya", "lesotho", "liberia", "libya", "madagascar",
        "malawi", "mali", "mauritania", "mauritius", "morocco", "mozambique",
        "namibia", "niger", "nigeria", "rwanda", "senegal", "sierra leone",
        "somalia", "south africa", "south sudan", "sudan", "tanzania", "togo",
        "tunisia", "uganda", "zambia", "zimbabwe",
    ],
    "Asia": [
        "afghanistan", "armenia", "azerbaijan", "bahrain", "bangladesh",
        "bhutan", "brunei", "cambodia", "china", "中国", "prc", "georgia",
        "hong kong", "india", "भारत", "indonesia", "iran", "iraq", "israel",
        "japan", "日本", "jordan", "kazakhstan", "kuwait", "kyrgyzstan", "laos",
        "lebanon", "macau", "malaysia", "maldives", "mongolia", "myanmar",
        "nepal", "north korea", "oman", "pakistan", "palestine", "philippines",
        "qatar", "saudi arabia", "singapore", "south korea", "korea", "한국",
        "sri lanka", "syria", "taiwan", "tajikistan", "thailand", "timor-leste",
        "turkey", "türkiye", "turkmenistan", "uae", "united arab emirates",
        "uzbekistan", "vietnam", "viet nam", "yemen",
    ],
    "Europe": [
        "albania", "andorra", "austria", "belarus", "belgium",
        "bosnia", "bulgaria", "croatia", "cyprus", "czech", "czechia",
        "denmark", "estonia", "finland", "france", "germany", "deutschland",
        "greece", "hungary", "iceland", "ireland", "italy", "italia", "kosovo",
        "latvia", "liechtenstein", "lithuania", "luxembourg", "malta",
        "moldova", "monaco", "montenegro", "netherlands", "north macedonia",
        "macedonia", "norway", "poland", "polska", "portugal", "romania",
        "russia", "россия", "san marino", "serbia", "slovakia", "slovenia",
        "spain", "españa", "sweden", "switzerland", "ukraine", "україна",
        "united kingdom", "uk", "u.k.", "england", "scotland", "wales",
        "great britain", "britain",
    ],
    "North America": [
        "canada", "costa rica", "cuba", "dominican republic", "el salvador",
        "guatemala", "haiti", "honduras", "jamaica", "mexico", "méxico",
        "nicaragua", "panama", "puerto rico", "trinidad", "united states",
        "usa", "u.s.a.", "u.s.", "america",
    ],
    "South America": [
        "argentina", "bolivia", "brazil", "brasil", "chile", "colombia",
        "ecuador", "guyana", "paraguay", "peru", "perú", "suriname", "uruguay",
        "venezuela",
    ],
    "Oceania": [
        "australia", "fiji", "new zealand", "papua new guinea", "samoa",
        "tonga", "vanuatu",
    ],
}

# major cities -> continent (helps when no country is given)
_CITIES = {
    "Africa": ["lagos", "cairo", "nairobi", "accra", "casablanca", "johannesburg",
               "cape town", "addis ababa", "kampala", "dakar", "tunis"],
    "Asia": ["tokyo", "beijing", "shanghai", "shenzhen", "guangzhou", "hangzhou",
             "bangalore", "bengaluru", "mumbai", "delhi", "new delhi", "hyderabad",
             "chennai", "pune", "kolkata", "seoul", "singapore", "jakarta",
             "bangkok", "manila", "kuala lumpur", "ho chi minh", "hanoi", "dubai",
             "tel aviv", "istanbul", "karachi", "lahore", "dhaka", "tehran",
             "riyadh", "taipei"],
    "Europe": ["london", "berlin", "munich", "hamburg", "paris", "madrid",
               "barcelona", "amsterdam", "rotterdam", "rome", "milan", "madrid",
               "lisbon", "porto", "vienna", "zurich", "geneva", "stockholm",
               "oslo", "copenhagen", "helsinki", "dublin", "brussels", "warsaw",
               "prague", "budapest", "bucharest", "athens", "moscow",
               "saint petersburg", "kyiv", "kiev", "manchester", "edinburgh"],
    "North America": ["new york", "nyc", "brooklyn", "san francisco", "sf",
                      "los angeles", "seattle", "boston", "chicago", "austin",
                      "denver", "atlanta", "miami", "toronto", "vancouver",
                      "montreal", "mexico city", "guadalajara", "bay area",
                      "silicon valley", "washington", "portland", "san diego",
                      "san jose", "dallas", "houston"],
    "South America": ["são paulo", "sao paulo", "rio de janeiro", "buenos aires",
                      "bogotá", "bogota", "lima", "santiago", "caracas",
                      "montevideo", "quito", "medellín", "medellin"],
    "Oceania": ["sydney", "melbourne", "brisbane", "perth", "auckland",
                "wellington"],
}

# US state abbreviations (", CA", ", NY") => North America
_US_STATES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
}

# Build a single ordered list of (needle, continent). Longer needles first so
# "south africa" wins over "africa"-like partials and "new york" over "york".
_NEEDLES = []
for _table in (_COUNTRIES, _CITIES):
    for _cont, _names in _table.items():
        for _n in _names:
            _NEEDLES.append((_n, _cont))
_NEEDLES.sort(key=lambda x: len(x[0]), reverse=True)

CONTINENTS = ["Africa", "Asia", "Europe", "North America", "South America",
              "Oceania", "Unknown"]


# India / Pakistan indicators (countries, aliases, major cities). Used to keep
# only these two when excluding the rest of Asia. Safe as substrings because
# this is only ever checked for locations already resolved to the Asia continent
# (so e.g. "Indiana, US" — North America — never reaches here).
_INDIA_PAKISTAN = {
    # India
    "india", "bharat", "भारत", "bengaluru", "bangalore", "mumbai", "bombay",
    "delhi", "new delhi", "hyderabad", "chennai", "madras", "pune", "kolkata",
    "calcutta", "ahmedabad", "jaipur", "kochi", "cochin", "noida", "gurgaon",
    "gurugram", "chandigarh", "lucknow", "indore", "coimbatore", "nagpur",
    "surat", "bhopal", "thiruvananthapuram", "trivandrum", "mysore", "mysuru",
    "visakhapatnam", "vijayawada", "kerala", "punjab", "gujarat", "maharashtra",
    "karnataka", "tamil nadu", "telangana",
    # Pakistan
    "pakistan", "karachi", "lahore", "islamabad", "rawalpindi", "faisalabad",
    "peshawar", "multan", "quetta", "sialkot", "gujranwala",
}


def is_india_or_pakistan(location: str | None) -> bool:
    """True if a location (already known to be in Asia) is India or Pakistan."""
    if not location:
        return False
    t = location.lower()
    return any(needle in t for needle in _INDIA_PAKISTAN)


# Canonical country name -> needles (name aliases + major cities/regions).
# Used to normalize free-text locations so "Brazil"/"Brasil"/"São Paulo" all
# roll up to one country. Longer needles are matched first (see _COUNTRY_NEEDLES).
_COUNTRY_GROUPS = {
    "Brazil": ["brazil", "brasil", "são paulo", "sao paulo", "rio de janeiro",
               "belo horizonte", "brasilia", "brasília", "curitiba",
               "porto alegre", "fortaleza", "recife", "salvador"],
    "India": ["india", "bharat", "भारत", "bengaluru", "bangalore", "mumbai",
              "bombay", "new delhi", "delhi", "hyderabad", "chennai", "madras",
              "pune", "kolkata", "calcutta", "ahmedabad", "jaipur", "kochi",
              "cochin", "noida", "gurgaon", "gurugram", "chandigarh", "lucknow",
              "indore", "coimbatore", "nagpur", "kerala", "punjab", "gujarat",
              "maharashtra", "karnataka", "tamil nadu", "telangana"],
    "Pakistan": ["pakistan", "karachi", "lahore", "islamabad", "rawalpindi",
                 "faisalabad", "peshawar", "multan", "quetta", "sialkot",
                 "gujranwala"],
    "Nigeria": ["nigeria", "lagos", "abuja", "ibadan", "kano", "port harcourt"],
    "Egypt": ["egypt", "cairo", "alexandria", "giza"],
    "Kenya": ["kenya", "nairobi", "mombasa"],
    "Ghana": ["ghana", "accra", "kumasi"],
    "South Africa": ["south africa", "johannesburg", "cape town", "durban", "pretoria"],
    "Morocco": ["morocco", "casablanca", "rabat", "marrakech"],
    "France": ["france", "paris", "lyon", "marseille", "toulouse", "bordeaux",
               "nantes", "lille", "strasbourg"],
    "Germany": ["germany", "deutschland", "berlin", "munich", "münchen",
                "hamburg", "frankfurt", "cologne", "köln", "stuttgart",
                "düsseldorf", "dusseldorf"],
    "Spain": ["spain", "españa", "espana", "madrid", "barcelona", "valencia",
              "seville", "sevilla", "bilbao", "málaga", "malaga"],
    "Portugal": ["portugal", "lisbon", "lisboa", "porto"],
    "Italy": ["italy", "italia", "rome", "roma", "milan", "milano", "turin", "naples"],
    "United Kingdom": ["united kingdom", "u.k.", "england", "scotland", "wales",
                       "great britain", "britain", "london", "manchester",
                       "edinburgh", "birmingham", "glasgow", "bristol", "leeds"],
    "Ireland": ["ireland", "dublin"],
    "Netherlands": ["netherlands", "amsterdam", "rotterdam", "the hague", "utrecht"],
    "Belgium": ["belgium", "brussels", "antwerp"],
    "Switzerland": ["switzerland", "zurich", "zürich", "geneva", "lausanne", "bern"],
    "Sweden": ["sweden", "stockholm", "gothenburg"],
    "Norway": ["norway", "oslo"],
    "Denmark": ["denmark", "copenhagen"],
    "Finland": ["finland", "helsinki"],
    "Poland": ["poland", "polska", "warsaw", "krakow", "kraków", "wrocław", "wroclaw"],
    "Czechia": ["czechia", "czech republic", "prague", "praha"],
    "Austria": ["austria", "vienna", "wien"],
    "Greece": ["greece", "athens"],
    "Romania": ["romania", "bucharest", "cluj"],
    "Ukraine": ["ukraine", "україна", "kyiv", "kiev", "lviv", "kharkiv"],
    "Russia": ["russia", "россия", "moscow", "saint petersburg", "st petersburg"],
    "Turkey": ["turkey", "türkiye", "turkiye", "istanbul", "ankara", "izmir"],
    "United States": ["united states", "u.s.a.", "u.s.", "usa", "new york",
                      "nyc", "brooklyn", "san francisco", "bay area",
                      "silicon valley", "los angeles", "seattle", "boston",
                      "chicago", "austin", "denver", "atlanta", "miami",
                      "portland", "san diego", "san jose", "dallas", "houston",
                      "washington"],
    "Canada": ["canada", "toronto", "vancouver", "montreal", "montréal",
               "ottawa", "calgary", "waterloo"],
    "Mexico": ["mexico", "méxico", "mexico city", "guadalajara", "monterrey"],
    "Argentina": ["argentina", "buenos aires", "córdoba", "cordoba", "rosario",
                  "mendoza", "la plata"],
    "Colombia": ["colombia", "bogotá", "bogota", "medellín", "medellin", "cali"],
    "Chile": ["chile", "santiago"],
    "Peru": ["peru", "perú", "lima"],
    "Uruguay": ["uruguay", "montevideo"],
    "Venezuela": ["venezuela", "caracas"],
    "Ecuador": ["ecuador", "quito", "guayaquil"],
    "Australia": ["australia", "sydney", "melbourne", "brisbane", "perth", "canberra"],
    "New Zealand": ["new zealand", "auckland", "wellington"],
}

_COUNTRY_NEEDLES = sorted(
    ((needle, country) for country, names in _COUNTRY_GROUPS.items() for needle in names),
    key=lambda x: len(x[0]), reverse=True,
)

# Canonical countries we can recognise, alphabetical (for the scrape filter UI).
COUNTRIES = sorted(_COUNTRY_GROUPS)


def country_of(location: str | None) -> str:
    """Normalize a free-text location to a canonical country name, or 'Unknown'."""
    if not location:
        return "Unknown"
    text = location.lower()
    for needle, country in _COUNTRY_NEEDLES:
        if needle in text:
            return country
    for part in text.replace("/", ",").split(","):
        if part.strip() in _US_STATES:
            return "United States"
    return "Unknown"


def continent_of(location: str | None) -> str:
    if not location:
        return "Unknown"
    text = location.lower()
    for needle, cont in _NEEDLES:
        if needle in text:
            return cont
    # ", CA" / ", NY" style US state suffix
    for part in text.replace("/", ",").split(","):
        if part.strip() in _US_STATES:
            return "North America"
    return "Unknown"
