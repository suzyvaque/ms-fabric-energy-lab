"""Reference lists for synthetic records. Names are common given/family names combined at random;
emails use example.com, phones use the fictional 555-01xx range, regions are generic."""

FIRST_NAMES = [
    "Alex", "Avery", "Bailey", "Cameron", "Casey", "Dakota", "Drew", "Elliot", "Emerson", "Finley",
    "Harper", "Hayden", "Jamie", "Jesse", "Jordan", "Kai", "Kendall", "Logan", "Morgan", "Parker",
    "Peyton", "Quinn", "Reese", "Riley", "Rowan", "Sage", "Sawyer", "Skyler", "Taylor", "Toni",
]
LAST_NAMES = [
    "Adams", "Baker", "Carter", "Diaz", "Evans", "Fischer", "Garcia", "Hughes", "Ito", "Jensen",
    "Kim", "Lopez", "Martin", "Nguyen", "Okafor", "Patel", "Quist", "Rivera", "Silva", "Tanaka",
    "Usman", "Vargas", "Walker", "Xu", "Young", "Zimmer", "Novak", "Moreau", "Rossi", "Larsen",
]
STREETS = ["Oak", "Maple", "Cedar", "Pine", "Elm", "Birch", "Willow", "Aspen", "Juniper", "Sunset",
           "Ridge", "Lakeview", "Meadow", "Canyon", "Harbor"]
STREET_TYPES = ["St", "Ave", "Rd", "Ln", "Dr", "Ct", "Way"]

# Generic regions. irr = irradiance multiplier, temp = mean ambient (C), clear = share of clear days.
REGIONS = {
    "Desert":   {"lat": (32.0, 35.5), "lon": (-115.0, -110.0), "irr": 1.15, "temp": 24.0, "clear": 0.80,
                 "tz": "America/Phoenix", "cities": ["Sunridge", "Dry Wells", "Mesa Park"]},
    "Coastal":  {"lat": (33.0, 38.0), "lon": (-122.5, -117.0), "irr": 1.00, "temp": 17.0, "clear": 0.60,
                 "tz": "America/Los_Angeles", "cities": ["Bayside", "Harbor Point", "Seacliff"]},
    "Valley":   {"lat": (35.5, 39.5), "lon": (-121.5, -118.5), "irr": 1.05, "temp": 19.0, "clear": 0.65,
                 "tz": "America/Los_Angeles", "cities": ["Orchard Hill", "Riverbend", "Greenfield"]},
    "Mountain": {"lat": (38.0, 41.0), "lon": (-109.0, -104.5), "irr": 1.00, "temp": 10.0, "clear": 0.55,
                 "tz": "America/Denver", "cities": ["Pinecrest", "Highmeadow", "Silver Creek"]},
    "Plains":   {"lat": (30.0, 34.0), "lon": (-101.0, -96.0), "irr": 1.02, "temp": 20.0, "clear": 0.55,
                 "tz": "America/Chicago", "cities": ["Prairie View", "Windmill", "Cottonwood"]},
}
REGION_WEIGHTS = {"Desert": 0.25, "Coastal": 0.25, "Valley": 0.2, "Mountain": 0.12, "Plains": 0.18}

DEVICE_MODELS = {
    "inverter": ["Contoso SunWave 5.0", "Contoso SunWave 7.6", "Contoso SunWave 11.4"],
    "battery": ["Contoso PowerCell 10", "Contoso PowerCell 13.5", "Contoso PowerCell 20"],
    "meter": ["Contoso GridSense M1", "Contoso GridSense M2"],
    "gateway": ["Contoso Link G2", "Contoso Link G3"],
}
FIRMWARE_V1 = ["1.8.2", "1.8.4", "1.9.0"]
FIRMWARE_V2 = ["2.1.0", "2.1.3"]

CREDIT_BANDS = {  # weight, risk score range, base APR, monthly late probability
    "A": (0.25, (780, 850), 3.99, 0.02),
    "B": (0.35, (720, 779), 5.49, 0.05),
    "C": (0.25, (660, 719), 7.49, 0.10),
    "D": (0.10, (600, 659), 9.99, 0.20),
    "E": (0.05, (540, 599), 12.49, 0.35),
}
DEFAULT_PROB = {"A": 0.002, "B": 0.01, "C": 0.03, "D": 0.10, "E": 0.25}
