"""airlines.py — ICAO callsign prefix to airline name/IATA mapping for Map in a Box.

Used by Shift+A overhead flights to convert raw OpenSky callsigns like
'QFA421' into readable 'Qantas QF421'.
"""

# ICAO_PREFIX -> (airline_name, IATA_code)
AIRLINES = {
    # Australia / NZ / Pacific
    "QFA": ("Qantas",               "QF"),
    "QLK": ("QantasLink",           "QF"),
    "VOZ": ("Virgin Australia",      "VA"),
    "JST": ("Jetstar",               "JQ"),
    "TGW": ("Tigerair Australia",    "TT"),
    "RXA": ("Rex Airlines",          "ZL"),
    "NZM": ("Air New Zealand",       "NZ"),
    "ANZ": ("Air New Zealand",       "NZ"),
    "FJI": ("Fiji Airways",          "FJ"),
    "AIC": ("Air India",             "AI"),
    "SLK": ("SilkAir",               "MI"),
    "PLQ": ("Pelican Airlines",      ""),
    "AWQ": ("Alliance Airlines",     "QQ"),
    "QQW": ("Alliance Airlines",     "QQ"),
    # Asia
    "SIA": ("Singapore Airlines",    "SQ"),
    "CPA": ("Cathay Pacific",        "CX"),
    "CCA": ("Air China",             "CA"),
    "CSN": ("China Southern",        "CZ"),
    "CES": ("China Eastern",         "MU"),
    "JAL": ("Japan Airlines",        "JL"),
    "ANA": ("All Nippon Airways",    "NH"),
    "KAL": ("Korean Air",            "KE"),
    "AAR": ("Asiana Airlines",       "OZ"),
    "THA": ("Thai Airways",          "TG"),
    "MAS": ("Malaysia Airlines",     "MH"),
    "GIA": ("Garuda Indonesia",      "GA"),
    "CEB": ("Cebu Pacific",          "5J"),
    "PAL": ("Philippine Airlines",   "PR"),
    "VJC": ("VietJet Air",           "VJ"),
    "HVN": ("Vietnam Airlines",      "VN"),
    "BAW": ("British Airways",       "BA"),  # also flies Asia
    "EVA": ("EVA Air",               "BR"),
    "CAL": ("China Airlines",        "CI"),
    "SHB": ("Shenzhen Airlines",     "ZH"),
    "HXA": ("Hainan Airlines",       "HU"),
    "XAX": ("AirAsia X",             "D7"),
    "AXB": ("AirAsia",               "AK"),
    "EZY": ("EasyJet",               "U2"),
    # Middle East
    "UAE": ("Emirates",              "EK"),
    "ETD": ("Etihad Airways",        "EY"),
    "QTR": ("Qatar Airways",         "QR"),
    "SVA": ("Saudia",                "SV"),
    "THY": ("Turkish Airlines",      "TK"),
    "ELY": ("El Al",                 "LY"),
    # Europe
    "DLH": ("Lufthansa",             "LH"),
    "AFR": ("Air France",            "AF"),
    "KLM": ("KLM",                   "KL"),
    "IBE": ("Iberia",                "IB"),
    "AZA": ("Alitalia/ITA Airways",  "AZ"),
    "SWR": ("Swiss",                 "LX"),
    "AUA": ("Austrian Airlines",     "OS"),
    "SAS": ("Scandinavian Airlines", "SK"),
    "FIN": ("Finnair",               "AY"),
    "RYR": ("Ryanair",               "FR"),
    "VLG": ("Vueling",               "VY"),
    "BEL": ("Brussels Airlines",     "SN"),
    "TAP": ("TAP Air Portugal",      "TP"),
    "LOT": ("LOT Polish Airlines",   "LO"),
    "CSA": ("Czech Airlines",        "OK"),
    "ROM": ("TAROM",                 "RO"),
    "AFL": ("Aeroflot",              "SU"),
    "NOZ": ("Norwegian",             "DY"),
    "WZZ": ("Wizz Air",              "W6"),
    # North America
    "AAL": ("American Airlines",     "AA"),
    "UAL": ("United Airlines",       "UA"),
    "DAL": ("Delta Air Lines",       "DL"),
    "SWA": ("Southwest Airlines",    "WN"),
    "ASA": ("Alaska Airlines",       "AS"),
    "JBU": ("JetBlue",               "B6"),
    "FFT": ("Frontier Airlines",     "F9"),
    "NKS": ("Spirit Airlines",       "NK"),
    "HAL": ("Hawaiian Airlines",     "HA"),
    "ACA": ("Air Canada",            "AC"),
    "WJA": ("WestJet",               "WS"),
    # South America
    "LAN": ("LATAM Airlines",        "LA"),
    "TAM": ("LATAM Brasil",          "JJ"),
    "GLO": ("Gol Airlines",          "G3"),
    "ARG": ("Aerolíneas Argentinas", "AR"),
    "AVA": ("Avianca",               "AV"),
    "COA": ("Copa Airlines",         "CM"),
    # Africa
    "SAA": ("South African Airways", "SA"),
    "ETH": ("Ethiopian Airlines",    "ET"),
    "KQA": ("Kenya Airways",         "KQ"),
    "EAL": ("EgyptAir",              "MS"),
    "RAM": ("Royal Air Maroc",       "AT"),
    # Cargo (common overhead)
    "FDX": ("FedEx",                 "FX"),
    "UPS": ("UPS Airlines",          "5X"),
    "GTI": ("Atlas Air",             "5Y"),
    "PAC": ("Polar Air Cargo",       "PO"),
}


def decode_callsign(raw: str) -> tuple[str, str]:
    """Convert raw OpenSky callsign to (airline_name, flight_number).
    
    e.g. 'QFA421' -> ('Qantas', 'QF421')
         'VOZ901' -> ('Virgin Australia', 'VA901')
         'UNKNOWN' -> ('', 'UNKNOWN')
    """
    raw = (raw or "").strip().upper()
    if not raw:
        return ("", "")

    # Try 3-letter prefix first, then 2-letter
    for prefix_len in (3, 2):
        prefix = raw[:prefix_len]
        if prefix in AIRLINES:
            name, iata = AIRLINES[prefix]
            number = raw[prefix_len:]
            flight_num = f"{iata}{number}" if iata else raw
            return (name, flight_num)

    # Unknown airline — return raw callsign
    return ("", raw)
