"""
TickerMover — Stock Universe (201 stocks, April 2026)
Source: US_Tech_Telecom_Infra_StockUniverse_v3.xlsx
Sectors: Technology · Telecom · Infrastructure ONLY (no healthcare/biotech)
Format: (ticker, name, sector, sub_sector, market_cap_tier, growth_tier, thesis)
"""
from __future__ import annotations

# ── Fast / Test universe ──────────────────────────────────────────────
FAST: list[str] = [
    # Removed $1T+ mega caps: NVDA, MSFT, GOOGL, META, AMZN, AAPL
    "AMD",  "AVGO", "ANET", "PLTR", "CRWD",
    "PANW", "ZS",   "NET",  "DDOG", "VRT",
    "ETN",  "GEV",  "CEG",  "VST",  "TSLA",
    "NFLX", "TTD",  "COHR", "LITE", "ASTS",
    "IONQ", "LSCC", "MRVL", "MU",   "AMAT",
    "LRCX", "KLAC", "CRM",  "NOW",  "SNOW",
]

# ── Exchange lookup ───────────────────────────────────────────────────
_EXCHANGE: dict[str, str] = {
    # AI Semiconductors (NVDA removed — >$1T)
    "AMD":"NASDAQ", "AVGO":"NASDAQ","QCOM":"NASDAQ",
    "INTC":"NASDAQ","MU":"NASDAQ", "AMAT":"NASDAQ","LRCX":"NASDAQ",
    "KLAC":"NASDAQ","MRVL":"NASDAQ","ARM":"NASDAQ","SMCI":"NASDAQ",
    "TXN":"NASDAQ", "ADI":"NASDAQ","NXPI":"NASDAQ","MCHP":"NASDAQ",
    "ON":"NASDAQ",  "STX":"NASDAQ","WDC":"NASDAQ","SNPS":"NASDAQ",
    "CDNS":"NASDAQ","KEYS":"NYSE", "ONTO":"NYSE",  "ENTG":"NASDAQ",
    "MKSI":"NASDAQ","QRVO":"NASDAQ","SWKS":"NASDAQ","CRDO":"NASDAQ",
    "ALAB":"NASDAQ","MPWR":"NASDAQ","AMBA":"NASDAQ","CAMT":"NASDAQ",
    "ASML":"NASDAQ",
    # Optical
    "COHR":"NYSE",  "LITE":"NASDAQ","FN":"NYSE",   "MTSI":"NASDAQ",
    "AAOI":"NASDAQ","VIAV":"NASDAQ",   # IIVI→COHR (merged 2022), NPTN acquired by Intel 2022
    # Agentic AI
    "PLTR":"NYSE",  "AI":"NYSE",   "PATH":"NYSE",  "BBAI":"NYSE",
    "SOUN":"NASDAQ","RXRX":"NASDAQ","TEM":"NASDAQ",
    # Quantum
    "IONQ":"NYSE",  "RGTI":"NASDAQ","QUBT":"NASDAQ","QMCO":"NASDAQ",
    "IBM":"NYSE",
    # Robotics
    "TSLA":"NASDAQ","ISRG":"NASDAQ","ACMR":"NASDAQ","BRKS":"NASDAQ",
    "ABBNY":"OTC",  "FANUY":"OTC",
    # Satellite
    "ASTS":"NASDAQ","VSAT":"NASDAQ","GSAT":"NYSE",  "SATS":"NASDAQ",
    "RKLB":"NASDAQ","IRDM":"NASDAQ",
    # AI Data Center / GPU Cloud
    "CRWV":"NASDAQ","APLD":"NASDAQ",
    # Cybersecurity
    "CRWD":"NASDAQ","PANW":"NASDAQ","ZS":"NASDAQ",  "OKTA":"NASDAQ",
    "S":"NYSE",     "FTNT":"NASDAQ","CYBR":"NASDAQ","TENB":"NASDAQ",
    "VRNS":"NASDAQ","QLYS":"NASDAQ","CHKP":"NASDAQ","SAIL":"NYSE",
    "KTOS":"NASDAQ","BAH":"NYSE",   "LDOS":"NYSE",  "SAIC":"NYSE",
    # Enterprise SaaS
    "CRM":"NYSE",   "NOW":"NYSE",   "SNOW":"NYSE",  "DDOG":"NASDAQ",
    "MDB":"NASDAQ", "NET":"NYSE",   "CFLT":"NASDAQ","ADSK":"NASDAQ",
    "ANSS":"NASDAQ","PTC":"NASDAQ", "VEEV":"NYSE",  "WDAY":"NASDAQ",
    "HUBS":"NYSE",  "GTLB":"NASDAQ","TEAM":"NASDAQ","DOCN":"NYSE",
    "ESTC":"NYSE",  "INTU":"NASDAQ","DT":"NYSE",    "ADBE":"NASDAQ",
    "MNDY":"NASDAQ","PAYC":"NYSE",  "BRZE":"NASDAQ","PCTY":"NASDAQ",
    # Cloud Hyperscalers
    # MSFT/AMZN/GOOGL/META/AAPL removed — all >$1T, replaced by better opportunities
    "ORCL":"NYSE",
    # Edge AI
    "LSCC":"NASDAQ","AKAM":"NASDAQ",
    # Networking
    "ANET":"NYSE",  "CSCO":"NASDAQ","JNPR":"NYSE",  "CIEN":"NYSE",
    "INFN":"NASDAQ","NTAP":"NASDAQ","PSTG":"NYSE",  "CALX":"NYSE",
    "ADTN":"NASDAQ",
    # Digital Payments
    "V":"NYSE",     "MA":"NYSE",    "PYPL":"NASDAQ","SQ":"NYSE",
    "AFRM":"NASDAQ","SOFI":"NASDAQ","COIN":"NASDAQ","HOOD":"NASDAQ",
    "NU":"NYSE",    "FI":"NASDAQ",  "FIS":"NYSE",   "AXP":"NYSE",
    "BILL":"NYSE",  "FOUR":"NYSE",
    # Digital Media
    "NFLX":"NASDAQ","DIS":"NYSE",   "SPOT":"NYSE",  "ROKU":"NASDAQ",
    "TTD":"NASDAQ", "APP":"NASDAQ", "MGNI":"NASDAQ","WBD":"NASDAQ",
    "PARA":"NASDAQ",
    # 5G & Fiber
    "TMUS":"NASDAQ","FYBR":"NASDAQ","LUMN":"NYSE",
    # Cell Towers
    "UNIT":"NASDAQ",
    # Data Center REITs
    "EQIX":"NASDAQ","DLR":"NYSE",   "AMT":"NYSE",   "IRM":"NYSE",
    "SBAC":"NASDAQ","CCI":"NYSE",   "VICI":"NYSE",  "PLD":"NYSE",
    # AI Power / Grid
    "ETN":"NYSE",   "GEV":"NYSE",   "VST":"NYSE",   "CEG":"NASDAQ",
    "NRG":"NYSE",   "TLN":"NASDAQ", "NEE":"NYSE",   "AES":"NYSE",
    "PCG":"NYSE",   "EXC":"NASDAQ", "BE":"NYSE",    "FSLR":"NASDAQ",
    "ENPH":"NASDAQ",
    # Thermal / Cooling
    "VRT":"NYSE",   "NVT":"NYSE",   "CARR":"NYSE",  "AAON":"NASDAQ",
    "HPE":"NYSE",   "DELL":"NYSE",
    # Industrial Automation
    "HON":"NASDAQ", "EMR":"NYSE",   "ROK":"NYSE",   "GE":"NYSE",
    "PH":"NYSE",    "AXON":"NASDAQ","BX":"NYSE",    "KKR":"NYSE",
    "APO":"NYSE",
    # Large-Cap Telecom
    "T":"NYSE",     "VZ":"NYSE",    "CMCSA":"NASDAQ","CHTR":"NASDAQ",
    # SiC / Wide Bandgap
    "AEHR":"NASDAQ","ACLS":"NASDAQ","WOLF":"NYSE",
    # Semiconductor Test Equipment
    "TER":"NASDAQ", "COHU":"NASDAQ","FORM":"NASDAQ",
    # AI Defense
    "CACI":"NYSE",  "LHX":"NYSE",
}

# ── Main universe (201 stocks, exactly matching spreadsheet) ──────────
# (ticker, name, sector, sub_sector, market_cap_tier, growth_tier, thesis)
HG_DATA: list[tuple] = [

    # ═══════════════════════════════════════════════════════════════
    #  AI SEMICONDUCTORS (33)
    # ═══════════════════════════════════════════════════════════════
    # NVDA (~$2.5T) and AVGO — removed: >$1T market cap; replaced by tradeable alternatives below
    ("AMD",  "Advanced Micro Devices",   "Technology",     "AI Semiconductors",              "Large Cap",  "🔥 Very High (>25% CAGR)", "MI300X GPU challenger; x86 CPU share gains"),
    ("QCOM", "Qualcomm Inc",             "Technology",     "AI Semiconductors",              "Large Cap",  "🔥 Very High (>25% CAGR)", "AI smartphones; Snapdragon X Elite; edge inference"),
    ("INTC", "Intel Corp",               "Technology",     "AI Semiconductors",              "Large Cap",  "🔥 Very High (>25% CAGR)", "Restructuring; Gaudi AI accelerator; foundry bet"),
    ("MU",   "Micron Technology",        "Technology",     "AI Semiconductors",              "Large Cap",  "🔥 Very High (>25% CAGR)", "HBM3E memory critical for AI GPUs"),
    ("AMAT", "Applied Materials",        "Technology",     "AI Semiconductors",              "Large Cap",  "🔥 Very High (>25% CAGR)", "Wafer fabrication equipment; packaging leader"),
    ("LRCX", "Lam Research",             "Technology",     "AI Semiconductors",              "Large Cap",  "🔥 Very High (>25% CAGR)", "Etch & deposition equipment; gate-all-around"),
    ("KLAC", "KLA Corp",                 "Technology",     "AI Semiconductors",              "Large Cap",  "🔥 Very High (>25% CAGR)", "Process control inspection; yield enhancement"),
    ("MRVL", "Marvell Technology",       "Technology",     "AI Semiconductors",              "Large Cap",  "🔥 Very High (>25% CAGR)", "Custom AI ASICs; data center networking silicon"),
    ("ARM",  "Arm Holdings",             "Technology",     "AI Semiconductors",              "Large Cap",  "🔥 Very High (>25% CAGR)", "IP licensor for every mobile/edge/AI chip"),
    ("SMCI", "Super Micro Computer",     "Technology",     "AI Semiconductors",              "Large Cap",  "🔥 Very High (>25% CAGR)", "AI server integration; liquid-cooled rack systems"),
    ("TXN",  "Texas Instruments",        "Technology",     "AI Semiconductors",              "Large Cap",  "🔥 Very High (>25% CAGR)", "Analog/embedded; industrial & automotive exposure"),
    ("ADI",  "Analog Devices",           "Technology",     "AI Semiconductors",              "Large Cap",  "🔥 Very High (>25% CAGR)", "High-performance analog; industrial automation"),
    ("NXPI", "NXP Semiconductors",       "Technology",     "AI Semiconductors",              "Large Cap",  "🔥 Very High (>25% CAGR)", "Automotive MCU + radar; ADAS exposure"),
    ("MCHP", "Microchip Technology",     "Technology",     "AI Semiconductors",              "Mid Cap",    "🔥 Very High (>25% CAGR)", "MCUs for IoT/industrial; dividend grower"),
    ("ON",   "ON Semiconductor",         "Technology",     "AI Semiconductors",              "Mid Cap",    "🔥 Very High (>25% CAGR)", "SiC power modules for EVs & solar inverters"),
    ("STX",  "Seagate Technology",       "Technology",     "AI Semiconductors",              "Mid Cap",    "🔥 Very High (>25% CAGR)", "Mass capacity HDD for AI training storage"),
    ("WDC",  "Western Digital",          "Technology",     "AI Semiconductors",              "Mid Cap",    "🔥 Very High (>25% CAGR)", "NAND flash + HDD; AI data storage plays"),
    ("SNPS", "Synopsys Inc",             "Technology",     "AI Semiconductors",              "Large Cap",  "🔥 Very High (>25% CAGR)", "EDA software; chip design automation"),
    ("CDNS", "Cadence Design Systems",   "Technology",     "AI Semiconductors",              "Large Cap",  "🔥 Very High (>25% CAGR)", "EDA + IP; mandatory for advanced chip design"),
    ("KEYS", "Keysight Technologies",    "Technology",     "AI Semiconductors",              "Mid Cap",    "🔥 Very High (>25% CAGR)", "Electronic test & measurement equipment"),
    ("ONTO", "Onto Innovation",          "Technology",     "AI Semiconductors",              "Small Cap",  "🔥 Very High (>25% CAGR)", "Metrology for advanced packaging & HBM"),
    ("ENTG", "Entegris Inc",             "Technology",     "AI Semiconductors",              "Mid Cap",    "🔥 Very High (>25% CAGR)", "Specialty materials & filtration for fabs"),
    ("MKSI", "MKS Instruments",          "Technology",     "AI Semiconductors",              "Mid Cap",    "🔥 Very High (>25% CAGR)", "Power, gas, vacuum equipment for fabs"),
    ("QRVO", "Qorvo Inc",                "Technology",     "AI Semiconductors",              "Mid Cap",    "🔥 Very High (>25% CAGR)", "RF chips for 5G smartphones"),
    ("SWKS", "Skyworks Solutions",       "Technology",     "AI Semiconductors",              "Mid Cap",    "🔥 Very High (>25% CAGR)", "RF chips; Apple supplier; IoT"),
    ("CRDO", "Credo Technology",         "Technology",     "AI Semiconductors",              "Small Cap",  "🔥 Very High (>25% CAGR)", "High-speed SerDes for AI data center interconnect"),
    ("ALAB", "Astera Labs",              "Technology",     "AI Semiconductors",              "Small Cap",  "🔥 Very High (>25% CAGR)", "CXL/PCIe connectivity for AI racks"),
    ("MPWR", "Monolithic Power Systems", "Technology",     "AI Semiconductors",              "Mid Cap",    "🔥 Very High (>25% CAGR)", "Power management ICs; AI server VRMs"),
    ("AMBA", "Ambarella Inc",            "Technology",     "AI Semiconductors",              "Small Cap",  "🔥 Very High (>25% CAGR)", "Edge AI vision chips; ADAS & security cameras"),
    ("CAMT", "Camtek Ltd",               "Technology",     "AI Semiconductors",              "Small Cap",  "🔥 Very High (>25% CAGR)", "Inspection for advanced packaging & HBM"),
    ("ASML", "ASML Holding NV",          "Technology",     "AI Semiconductors",              "Large Cap",  "🔥 Very High (>25% CAGR)", "Only maker of EUV lithography machines; every advanced AI chip depends on ASML"),

    # ═══════════════════════════════════════════════════════════════
    #  OPTICAL COMPONENTS / PHOTONICS (8)
    # ═══════════════════════════════════════════════════════════════
    ("COHR", "Coherent Corp",            "Technology",     "Optical Components / Photonics", "Large Cap",  "🔥 Very High (>25% CAGR)", "800G/1.6T transceivers for AI clusters; SiC wafers; surging in 2026"),
    ("LITE", "Lumentum Holdings",        "Technology",     "Optical Components / Photonics", "Mid Cap",    "🔥 Very High (>25% CAGR)", "800G datacom lasers for NVIDIA AI clusters; 40%+ rev growth"),
    ("FN",   "Fabrinet",                 "Technology",     "Optical Components / Photonics", "Mid Cap",    "🔥 Very High (>25% CAGR)", "Contract mfg for optical components; COHR/LITE primary partner"),
    ("MTSI", "MACOM Technology Solutions","Technology",    "Optical Components / Photonics", "Small Cap",  "🔥 Very High (>25% CAGR)", "GaAs/InP chips for coherent optics & 5G; co-packaged optics (CPO)"),
    ("AAOI", "Applied Optoelectronics",  "Technology",     "Optical Components / Photonics", "Small Cap",  "🔥 Very High (>25% CAGR)", "Optical transceivers; data center & cable TV; high leverage to AI"),
    # IIVI → now trades as COHR (merged Jan 2022); NPTN acquired by Intel (Aug 2022) — both delisted
    ("VIAV", "Viavi Solutions",          "Technology",     "Optical Components / Photonics", "Small Cap",  "🔥 Very High (>25% CAGR)", "Optical test & measurement; network visibility"),

    # ═══════════════════════════════════════════════════════════════
    #  AGENTIC AI / AI SOFTWARE (7)
    # ═══════════════════════════════════════════════════════════════
    ("PLTR", "Palantir Technologies",    "Technology",     "Agentic AI / AI Software",       "Large Cap",  "🔥 Very High (>25% CAGR)", "AIP platform; government + enterprise AI"),
    ("AI",   "C3.ai Inc",               "Technology",     "Agentic AI / AI Software",       "Small Cap",  "🔥 Very High (>25% CAGR)", "Enterprise AI applications suite"),
    ("PATH", "UiPath Inc",               "Technology",     "Agentic AI / AI Software",       "Mid Cap",    "🔥 Very High (>25% CAGR)", "Agentic process automation; RPA leader"),
    ("BBAI", "BigBear.ai Holdings",      "Technology",     "Agentic AI / AI Software",       "Micro Cap",  "🔥 Very High (>25% CAGR)", "Decision intelligence for defense/gov"),
    ("SOUN", "SoundHound AI",            "Technology",     "Agentic AI / AI Software",       "Micro Cap",  "🔥 Very High (>25% CAGR)", "Voice AI for automotive & restaurants"),
    ("RXRX", "Recursion Pharmaceuticals","Technology",     "Agentic AI / AI Software",       "Small Cap",  "🔥 Very High (>25% CAGR)", "AI drug discovery; TechBio platform"),
    ("TEM",  "Tempus AI Inc",            "Technology",     "Agentic AI / AI Software",       "Mid Cap",    "🔥 Very High (>25% CAGR)", "Clinical AI: genomic sequencing + AI diagnostics; 2,300 hospitals"),

    # ═══════════════════════════════════════════════════════════════
    #  QUANTUM COMPUTING (5)
    # ═══════════════════════════════════════════════════════════════
    ("IONQ", "IonQ Inc",                 "Technology",     "Quantum Computing",              "Small Cap",  "🔥 Very High (>25% CAGR)", "Trapped-ion quantum computers; cloud access"),
    ("RGTI", "Rigetti Computing",        "Technology",     "Quantum Computing",              "Micro Cap",  "🔥 Very High (>25% CAGR)", "Superconducting quantum processors"),
    ("QUBT", "Quantum Computing Inc",    "Technology",     "Quantum Computing",              "Micro Cap",  "🔥 Very High (>25% CAGR)", "Quantum optimization & sensing"),
    ("QMCO", "Quantum Corporation",      "Technology",     "Quantum Computing",              "Micro Cap",  "🔥 Very High (>25% CAGR)", "Data management + quantum storage research"),
    ("IBM",  "IBM Corp",                 "Technology",     "Quantum Computing",              "Large Cap",  "🔥 Very High (>25% CAGR)", "IBM Quantum; 2026 practical quantum milestone"),

    # ═══════════════════════════════════════════════════════════════
    #  ROBOTICS / PHYSICAL AI (6)
    # ═══════════════════════════════════════════════════════════════
    ("TSLA", "Tesla Inc",                "Technology",     "Robotics / Physical AI",         "Large Cap",  "🔥 Very High (>25% CAGR)", "Optimus humanoid; FSD autonomy; Dojo"),
    ("ISRG", "Intuitive Surgical",       "Technology",     "Robotics / Physical AI",         "Large Cap",  "🔥 Very High (>25% CAGR)", "da Vinci surgical robots; dominant moat"),
    ("ACMR", "ACM Research",             "Technology",     "Robotics / Physical AI",         "Small Cap",  "🔥 Very High (>25% CAGR)", "Wet cleaning equipment for advanced fabs"),
    ("BRKS", "Brooks Automation / Azenta","Technology",    "Robotics / Physical AI",         "Small Cap",  "🔥 Very High (>25% CAGR)", "Semiconductor robotics + life-sci storage"),
    ("ABBNY","ABB Ltd (ADR)",            "Technology",     "Robotics / Physical AI",         "Large Cap",  "🔥 Very High (>25% CAGR)", "Industrial robots; collaborative automation"),
    ("FANUY","FANUC Corp (ADR)",          "Technology",     "Robotics / Physical AI",         "Large Cap",  "🔥 Very High (>25% CAGR)", "Japanese CNC + industrial robot leader (ADR)"),

    # ═══════════════════════════════════════════════════════════════
    #  SATELLITE / SPACE COMMUNICATIONS (6)
    # ═══════════════════════════════════════════════════════════════
    ("ASTS", "AST SpaceMobile",          "Telecom",        "Satellite / Space Communications","Small Cap",  "🔥 Very High (>25% CAGR)", "Direct-to-device cellular via LEO satellites"),
    ("VSAT", "Viasat Inc",               "Telecom",        "Satellite / Space Communications","Mid Cap",    "🔥 Very High (>25% CAGR)", "Geostationary + LEO broadband; aviation"),
    ("GSAT", "Globalstar Inc",           "Telecom",        "Satellite / Space Communications","Small Cap",  "🔥 Very High (>25% CAGR)", "iPhone satellite SOS; Band 53/n53 network"),
    ("SATS", "EchoStar Corp",            "Telecom",        "Satellite / Space Communications","Mid Cap",    "🔥 Very High (>25% CAGR)", "Satellite broadband + 5G spectrum holdings"),
    ("RKLB", "Rocket Lab USA",           "Telecom",        "Satellite / Space Communications","Small Cap",  "🔥 Very High (>25% CAGR)", "Small launch vehicle + satellite manufacturing"),
    ("IRDM", "Iridium Communications",   "Telecom",        "Satellite / Space Communications","Mid Cap",    "🔥 Very High (>25% CAGR)", "LEO constellation; IoT + aviation + maritime"),

    # ═══════════════════════════════════════════════════════════════
    #  AI DATA CENTER / GPU CLOUD (2)
    # ═══════════════════════════════════════════════════════════════
    ("CRWV", "CoreWeave Inc",            "Technology",     "AI Data Center / GPU Cloud",     "Large Cap",  "🔥 Very High (>25% CAGR)", "Largest GPU cloud; $6.8B Anthropic deal + $21B Meta deal; pure-play hyperscaler for AI"),
    ("APLD", "Applied Digital Corp",     "Technology",     "AI Data Center / GPU Cloud",     "Small Cap",  "🔥 Very High (>25% CAGR)", "AI data center builder & HPC cloud; 1.5 GW pipeline"),

    # ═══════════════════════════════════════════════════════════════
    #  CYBERSECURITY (16)
    # ═══════════════════════════════════════════════════════════════
    ("CRWD", "CrowdStrike Holdings",     "Technology",     "Cybersecurity",                  "Large Cap",  "⬆  High (15-25% CAGR)",   "Cloud-native endpoint; Falcon platform"),
    ("PANW", "Palo Alto Networks",       "Technology",     "Cybersecurity",                  "Large Cap",  "⬆  High (15-25% CAGR)",   "AI-powered SASE/XDR; platformisation"),
    ("ZS",   "Zscaler Inc",             "Technology",     "Cybersecurity",                  "Large Cap",  "⬆  High (15-25% CAGR)",   "Zero-trust cloud security; SSE leader"),
    ("OKTA", "Okta Inc",                "Technology",     "Cybersecurity",                  "Mid Cap",    "⬆  High (15-25% CAGR)",   "Identity & access management; SSO"),
    ("S",    "SentinelOne Inc",          "Technology",     "Cybersecurity",                  "Mid Cap",    "⬆  High (15-25% CAGR)",   "AI-native EDR/XDR; Purple AI agent"),
    ("FTNT", "Fortinet Inc",             "Technology",     "Cybersecurity",                  "Large Cap",  "⬆  High (15-25% CAGR)",   "Network firewall + SASE; ASIC advantage"),
    ("CYBR", "CyberArk Software",        "Technology",     "Cybersecurity",                  "Mid Cap",    "⬆  High (15-25% CAGR)",   "Privileged access management leader"),
    ("TENB", "Tenable Holdings",         "Technology",     "Cybersecurity",                  "Small Cap",  "⬆  High (15-25% CAGR)",   "Exposure management; vulnerability scanning"),
    ("VRNS", "Varonis Systems",          "Technology",     "Cybersecurity",                  "Small Cap",  "⬆  High (15-25% CAGR)",   "Data security & governance; SaaS transition"),
    ("QLYS", "Qualys Inc",               "Technology",     "Cybersecurity",                  "Small Cap",  "⬆  High (15-25% CAGR)",   "Cloud-based security & compliance"),
    ("CHKP", "Check Point Software",     "Technology",     "Cybersecurity",                  "Large Cap",  "⬆  High (15-25% CAGR)",   "Network security veteran; consistent FCF"),
    ("SAIL", "SailPoint Technologies",   "Technology",     "Cybersecurity",                  "Mid Cap",    "⬆  High (15-25% CAGR)",   "Identity governance; AI-driven IGA"),
    ("KTOS", "Kratos Defense",           "Technology",     "Cybersecurity",                  "Small Cap",  "⬆  High (15-25% CAGR)",   "Cyber + unmanned systems for DoD"),
    ("BAH",  "Booz Allen Hamilton",      "Technology",     "Cybersecurity",                  "Large Cap",  "⬆  High (15-25% CAGR)",   "Gov IT + cyber consulting; AI modernisation"),
    ("LDOS", "Leidos Holdings",          "Technology",     "Cybersecurity",                  "Large Cap",  "⬆  High (15-25% CAGR)",   "Defense IT; cyber & AI for gov"),
    ("SAIC", "Science Applications Intl","Technology",     "Cybersecurity",                  "Large Cap",  "⬆  High (15-25% CAGR)",   "Gov IT services; cyber & digital transformation"),

    # ═══════════════════════════════════════════════════════════════
    #  ENTERPRISE SAAS / CLOUD SOFTWARE (24)
    # ═══════════════════════════════════════════════════════════════
    ("CRM",  "Salesforce Inc",           "Technology",     "Enterprise SaaS / Cloud Software","Large Cap",  "⬆  High (15-25% CAGR)",   "CRM + Agentforce AI; Data Cloud"),
    ("NOW",  "ServiceNow Inc",           "Technology",     "Enterprise SaaS / Cloud Software","Large Cap",  "⬆  High (15-25% CAGR)",   "IT workflow automation + Now AI agents"),
    ("SNOW", "Snowflake Inc",            "Technology",     "Enterprise SaaS / Cloud Software","Large Cap",  "⬆  High (15-25% CAGR)",   "Data cloud; Cortex AI; consumption model"),
    ("DDOG", "Datadog Inc",              "Technology",     "Enterprise SaaS / Cloud Software","Large Cap",  "⬆  High (15-25% CAGR)",   "Observability + AI monitoring; platform growth"),
    ("MDB",  "MongoDB Inc",              "Technology",     "Enterprise SaaS / Cloud Software","Mid Cap",    "⬆  High (15-25% CAGR)",   "Document DB; AI-app developer favourite"),
    ("NET",  "Cloudflare Inc",           "Technology",     "Enterprise SaaS / Cloud Software","Large Cap",  "⬆  High (15-25% CAGR)",   "Network edge + SASE + Workers AI"),
    ("CFLT", "Confluent Inc",            "Technology",     "Enterprise SaaS / Cloud Software","Mid Cap",    "⬆  High (15-25% CAGR)",   "Real-time data streaming; Kafka managed"),
    ("ADSK", "Autodesk Inc",             "Technology",     "Enterprise SaaS / Cloud Software","Large Cap",  "⬆  High (15-25% CAGR)",   "AEC + manufacturing design; AI generative"),
    ("ANSS", "ANSYS Inc",                "Technology",     "Enterprise SaaS / Cloud Software","Large Cap",  "⬆  High (15-25% CAGR)",   "Simulation software; acquiree of Synopsys"),
    ("PTC",  "PTC Inc",                  "Technology",     "Enterprise SaaS / Cloud Software","Mid Cap",    "⬆  High (15-25% CAGR)",   "Industrial IoT + PLM; ServiceMax"),
    ("VEEV", "Veeva Systems",            "Technology",     "Enterprise SaaS / Cloud Software","Large Cap",  "⬆  High (15-25% CAGR)",   "Life-sciences cloud; CRM + data"),
    ("WDAY", "Workday Inc",              "Technology",     "Enterprise SaaS / Cloud Software","Large Cap",  "⬆  High (15-25% CAGR)",   "HR + Finance cloud; AI workforce planning"),
    ("HUBS", "HubSpot Inc",              "Technology",     "Enterprise SaaS / Cloud Software","Large Cap",  "⬆  High (15-25% CAGR)",   "SMB CRM + marketing; Breeze AI"),
    ("GTLB", "GitLab Inc",               "Technology",     "Enterprise SaaS / Cloud Software","Mid Cap",    "⬆  High (15-25% CAGR)",   "DevSecOps platform; AI code review"),
    ("TEAM", "Atlassian Corp",           "Technology",     "Enterprise SaaS / Cloud Software","Large Cap",  "⬆  High (15-25% CAGR)",   "Jira + Confluence; Rovo AI agents"),
    ("DOCN", "DigitalOcean Holdings",    "Technology",     "Enterprise SaaS / Cloud Software","Small Cap",  "⬆  High (15-25% CAGR)",   "SMB cloud; GPU cloud for AI startups"),
    ("ESTC", "Elastic NV",               "Technology",     "Enterprise SaaS / Cloud Software","Mid Cap",    "⬆  High (15-25% CAGR)",   "Search + observability; ESRE AI search"),
    # INTU removed — Mega Cap >$180B; replaced by higher-alpha SaaS names

    ("DT",   "Dynatrace Inc",            "Technology",     "Enterprise SaaS / Cloud Software","Large Cap",  "⬆  High (15-25% CAGR)",   "AI-powered observability & security; Davis AI"),
    ("ADBE", "Adobe Inc",                "Technology",     "Enterprise SaaS / Cloud Software","Large Cap",  "⬆  High (15-25% CAGR)",   "Firefly generative AI; Creative Cloud + Document Cloud"),
    ("MNDY", "Monday.com Ltd",           "Technology",     "Enterprise SaaS / Cloud Software","Mid Cap",    "⬆  High (15-25% CAGR)",   "AI-powered work OS; 225K+ customers"),
    ("PAYC", "Paycom Software",          "Technology",     "Enterprise SaaS / Cloud Software","Mid Cap",    "➡  Moderate (8-15% CAGR)", "HCM SaaS; BETI AI payroll automation"),
    ("BRZE", "Braze Inc",                "Technology",     "Enterprise SaaS / Cloud Software","Small Cap",  "⬆  High (15-25% CAGR)",   "AI-powered customer engagement platform; 2,100+ brands"),
    ("PCTY", "Paylocity Holding Corp",   "Technology",     "Enterprise SaaS / Cloud Software","Mid Cap",    "➡  Moderate (8-15% CAGR)", "Cloud HCM & payroll; AI-powered Talent modules"),

    # ═══════════════════════════════════════════════════════════════
    #  CLOUD / HYPERSCALERS (1 remaining after removing >$1T)
    #  MSFT/AMZN/GOOGL/META/AAPL all removed — market cap >$1T
    # ═══════════════════════════════════════════════════════════════
    ("ORCL", "Oracle Corp",              "Technology",     "Cloud / Hyperscalers",           "Large Cap",  "⬆  High (15-25% CAGR)",   "OCI cloud + database; $21B AI training contracts"),

    # ═══════════════════════════════════════════════════════════════
    #  EDGE AI / EDGE COMPUTING (2)
    # ═══════════════════════════════════════════════════════════════
    ("LSCC", "Lattice Semiconductor",    "Technology",     "Edge AI / Edge Computing",       "Small Cap",  "🔥 Very High (>25% CAGR)", "Low-power FPGAs dominant in edge AI inference; server management"),
    ("AKAM", "Akamai Technologies",      "Technology",     "Edge AI / Edge Computing",       "Large Cap",  "➡  Moderate (8-15% CAGR)", "World's largest edge CDN + security; AI inference at the edge"),

    # ═══════════════════════════════════════════════════════════════
    #  NETWORKING & SWITCH INFRASTRUCTURE (9)
    # ═══════════════════════════════════════════════════════════════
    ("ANET", "Arista Networks",          "Technology",     "Networking & Switch Infrastructure","Large Cap", "➡  Moderate (8-15% CAGR)", "AI cluster 800G ethernet switching; dominant hyperscaler position"),
    ("CSCO", "Cisco Systems",            "Technology",     "Networking & Switch Infrastructure","Large Cap", "➡  Moderate (8-15% CAGR)", "Network + security + observability; Splunk acquisition; AI infra"),
    ("JNPR", "Juniper Networks",         "Technology",     "Networking & Switch Infrastructure","Large Cap", "➡  Moderate (8-15% CAGR)", "AI-driven networking; HPE acquisition pending"),
    ("CIEN", "Ciena Corp",               "Technology",     "Networking & Switch Infrastructure","Mid Cap",   "➡  Moderate (8-15% CAGR)", "Coherent optical transport for hyperscaler AI networks"),
    ("INFN", "Infinera Corp",            "Technology",     "Networking & Switch Infrastructure","Small Cap", "➡  Moderate (8-15% CAGR)", "Optical transport networking; ICE-X coherent"),
    ("NTAP", "NetApp Inc",               "Technology",     "Networking & Switch Infrastructure","Large Cap", "➡  Moderate (8-15% CAGR)", "Flash storage + cloud data services; AI storage"),
    ("PSTG", "Pure Storage Inc",         "Technology",     "Networking & Switch Infrastructure","Large Cap", "➡  Moderate (8-15% CAGR)", "All-flash array; AI training storage; Pure//E"),
    ("CALX", "Calix Inc",                "Technology",     "Networking & Switch Infrastructure","Small Cap", "➡  Moderate (8-15% CAGR)", "Broadband access platform for community ISPs"),
    ("ADTN", "ADTRAN Holdings",          "Technology",     "Networking & Switch Infrastructure","Small Cap", "➡  Moderate (8-15% CAGR)", "Fiber access equipment for ISPs"),

    # ═══════════════════════════════════════════════════════════════
    #  5G & FIBER INFRASTRUCTURE (4)
    # ═══════════════════════════════════════════════════════════════
    ("TMUS", "T-Mobile US",              "Telecom",        "5G & Fiber Infrastructure",      "Large Cap",  "➡  Moderate (8-15% CAGR)", "5G leader; FWA disrupting cable; mid-band"),
    ("FYBR", "Frontier Communications",  "Telecom",        "5G & Fiber Infrastructure",      "Mid Cap",    "➡  Moderate (8-15% CAGR)", "Fiber-to-the-home buildout; Verizon acquisition"),
    ("LUMN", "Lumen Technologies",       "Telecom",        "5G & Fiber Infrastructure",      "Small Cap",  "➡  Moderate (8-15% CAGR)", "Fiber network; restructuring + AI connectivity"),
    ("UNIT", "Uniti Group",              "Infrastructure", "5G & Fiber Infrastructure",      "Small Cap",  "➡  Moderate (8-15% CAGR)", "Fiber network; tower leasing"),

    # ═══════════════════════════════════════════════════════════════
    #  LARGE-CAP TELECOM CARRIERS (4)
    # ═══════════════════════════════════════════════════════════════
    ("T",    "AT&T Inc",                 "Telecom",        "Large-Cap Telecom (Carriers)",   "Large Cap",  "⬇  Stable (<8% CAGR)",    "Fiber + wireless carrier; deleveraging"),
    ("VZ",   "Verizon Communications",   "Telecom",        "Large-Cap Telecom (Carriers)",   "Large Cap",  "⬇  Stable (<8% CAGR)",    "Wireless network; Frontier fiber acquisition"),
    ("CMCSA","Comcast Corp",             "Telecom",        "Large-Cap Telecom (Carriers)",   "Large Cap",  "⬇  Stable (<8% CAGR)",    "Cable + Peacock streaming + NBCUniversal"),
    ("CHTR", "Charter Communications",  "Telecom",        "Large-Cap Telecom (Carriers)",   "Large Cap",  "⬇  Stable (<8% CAGR)",    "Cable + internet; mobile MVNO; FWA defence"),

    # ═══════════════════════════════════════════════════════════════
    #  DATA CENTER REITs (8)
    # ═══════════════════════════════════════════════════════════════
    ("EQIX", "Equinix Inc",              "Infrastructure", "Data Center REITs",              "Large Cap",  "🔥 Very High (>25% CAGR)", "World's largest DC REIT; 270+ facilities; interconnect"),
    ("DLR",  "Digital Realty Trust",     "Infrastructure", "Data Center REITs",              "Large Cap",  "🔥 Very High (>25% CAGR)", "300+ facilities; hyperscaler co-location"),
    ("AMT",  "American Tower Corp",      "Infrastructure", "Data Center REITs",              "Large Cap",  "🔥 Very High (>25% CAGR)", "Tower REIT + CoreSite data centers globally"),
    ("IRM",  "Iron Mountain Inc",        "Infrastructure", "Data Center REITs",              "Large Cap",  "🔥 Very High (>25% CAGR)", "Secure storage → data centers; 30% DC growth"),
    ("SBAC", "SBA Communications",       "Infrastructure", "Data Center REITs",              "Mid Cap",    "🔥 Very High (>25% CAGR)", "Wireless tower REIT; Americas + Africa"),
    ("CCI",  "Crown Castle Inc",         "Infrastructure", "Data Center REITs",              "Large Cap",  "🔥 Very High (>25% CAGR)", "US tower + fiber + small cells"),
    ("VICI", "VICI Properties",          "Infrastructure", "Data Center REITs",              "Large Cap",  "🔥 Very High (>25% CAGR)", "Gaming + experiential + data center pivot"),
    ("PLD",  "Prologis Inc",             "Infrastructure", "Data Center REITs",              "Large Cap",  "🔥 Very High (>25% CAGR)", "Industrial + data center logistics REIT"),

    # ═══════════════════════════════════════════════════════════════
    #  AI POWER / GRID INFRASTRUCTURE (13)
    # ═══════════════════════════════════════════════════════════════
    ("ETN",  "Eaton Corp",               "Infrastructure", "AI Power / Grid Infrastructure", "Large Cap",  "🔥 Very High (>25% CAGR)", "Power management + data center UPS/PDU; #1 pick"),
    ("GEV",  "GE Vernova",               "Infrastructure", "AI Power / Grid Infrastructure", "Large Cap",  "🔥 Very High (>25% CAGR)", "Grid equipment + turbines + wind; power bottleneck"),
    ("VST",  "Vistra Energy",            "Infrastructure", "AI Power / Grid Infrastructure", "Large Cap",  "🔥 Very High (>25% CAGR)", "Nuclear + gas power; data center PPA contracts"),
    ("CEG",  "Constellation Energy",     "Infrastructure", "AI Power / Grid Infrastructure", "Large Cap",  "🔥 Very High (>25% CAGR)", "Nuclear fleet restart; Google/Amazon PPAs"),
    ("NRG",  "NRG Energy",               "Infrastructure", "AI Power / Grid Infrastructure", "Mid Cap",    "🔥 Very High (>25% CAGR)", "Retail electricity + generation; data center"),
    ("TLN",  "Talen Energy",             "Infrastructure", "AI Power / Grid Infrastructure", "Mid Cap",    "🔥 Very High (>25% CAGR)", "Nuclear + gas; Susquehanna DC campus deal"),
    ("NEE",  "NextEra Energy",           "Infrastructure", "AI Power / Grid Infrastructure", "Large Cap",  "🔥 Very High (>25% CAGR)", "Largest US utility; wind + solar + storage"),
    ("AES",  "AES Corp",                 "Infrastructure", "AI Power / Grid Infrastructure", "Mid Cap",    "🔥 Very High (>25% CAGR)", "Renewables + storage; data center PPAs"),
    ("PCG",  "PG&E Corp",                "Infrastructure", "AI Power / Grid Infrastructure", "Large Cap",  "🔥 Very High (>25% CAGR)", "California utility; wildfire recovery; EV grid"),
    ("EXC",  "Exelon Corp",              "Infrastructure", "AI Power / Grid Infrastructure", "Large Cap",  "🔥 Very High (>25% CAGR)", "Regulated utility; nuclear fleet; grid investment"),
    ("BE",   "Bloom Energy",             "Infrastructure", "AI Power / Grid Infrastructure", "Small Cap",  "🔥 Very High (>25% CAGR)", "Solid oxide fuel cells for data center power"),
    ("FSLR", "First Solar Inc",          "Infrastructure", "AI Power / Grid Infrastructure", "Mid Cap",    "🔥 Very High (>25% CAGR)", "Thin-film solar panels; US manufacturing"),
    ("ENPH", "Enphase Energy",           "Infrastructure", "AI Power / Grid Infrastructure", "Mid Cap",    "🔥 Very High (>25% CAGR)", "Microinverters + battery storage; residential"),

    # ═══════════════════════════════════════════════════════════════
    #  THERMAL / COOLING MANAGEMENT (6)
    # ═══════════════════════════════════════════════════════════════
    ("VRT",  "Vertiv Holdings",          "Infrastructure", "Thermal / Cooling Management",   "Large Cap",  "🔥 Very High (>25% CAGR)", "Critical power + liquid cooling for AI racks; #1"),
    ("NVT",  "nVent Electric",           "Infrastructure", "Thermal / Cooling Management",   "Mid Cap",    "🔥 Very High (>25% CAGR)", "High-density PDUs + thermal mgmt for AI DCs"),
    ("CARR", "Carrier Global",           "Infrastructure", "Thermal / Cooling Management",   "Large Cap",  "🔥 Very High (>25% CAGR)", "HVAC + data center cooling; Viessmann acquisition"),
    ("AAON", "AAON Inc",                 "Infrastructure", "Thermal / Cooling Management",   "Small Cap",  "🔥 Very High (>25% CAGR)", "Commercial HVAC for data centers; niche leader"),
    ("HPE",  "Hewlett Packard Enterprise","Infrastructure","Thermal / Cooling Management",   "Large Cap",  "🔥 Very High (>25% CAGR)", "AI server + liquid-cooled Cray systems"),
    ("DELL", "Dell Technologies",        "Infrastructure", "Thermal / Cooling Management",   "Large Cap",  "🔥 Very High (>25% CAGR)", "AI PowerEdge servers; liquid cooling integration"),

    # ═══════════════════════════════════════════════════════════════
    #  INDUSTRIAL AUTOMATION / SMART GRID (9)
    # ═══════════════════════════════════════════════════════════════
    ("HON",  "Honeywell International",  "Infrastructure", "Industrial Automation / Smart Grid","Large Cap", "➡  Moderate (8-15% CAGR)", "Industrial IoT + building automation + aerospace"),
    ("EMR",  "Emerson Electric",         "Infrastructure", "Industrial Automation / Smart Grid","Large Cap", "➡  Moderate (8-15% CAGR)", "Process automation + software (AspenTech)"),
    ("ROK",  "Rockwell Automation",      "Infrastructure", "Industrial Automation / Smart Grid","Large Cap", "➡  Moderate (8-15% CAGR)", "Factory automation + industrial software"),
    ("GE",   "GE Aerospace",             "Infrastructure", "Industrial Automation / Smart Grid","Large Cap", "➡  Moderate (8-15% CAGR)", "Jet engines + defense electronics; post-spin"),
    ("PH",   "Parker Hannifin",          "Infrastructure", "Industrial Automation / Smart Grid","Large Cap", "➡  Moderate (8-15% CAGR)", "Motion & control; aerospace + industrial"),
    ("AXON", "Axon Enterprise",          "Infrastructure", "Industrial Automation / Smart Grid","Large Cap", "➡  Moderate (8-15% CAGR)", "Taser + body cams + AI evidence software"),
    ("BX",   "Blackstone Inc",           "Infrastructure", "Industrial Automation / Smart Grid","Large Cap", "➡  Moderate (8-15% CAGR)", "Alt asset manager; data center real estate"),
    ("KKR",  "KKR & Co",                 "Infrastructure", "Industrial Automation / Smart Grid","Large Cap", "➡  Moderate (8-15% CAGR)", "Alt asset manager; infra + tech investments"),
    ("APO",  "Apollo Global Management", "Infrastructure", "Industrial Automation / Smart Grid","Large Cap", "➡  Moderate (8-15% CAGR)", "Alt asset manager; credit + data center infra"),

    # ═══════════════════════════════════════════════════════════════
    #  DIGITAL PAYMENTS / FINTECH (14)
    # ═══════════════════════════════════════════════════════════════
    ("V",    "Visa Inc",                 "Technology",     "Digital Payments / Fintech",     "Large Cap",  "➡  Moderate (8-15% CAGR)", "Global payments network; volume toll road"),
    ("MA",   "Mastercard Inc",           "Technology",     "Digital Payments / Fintech",     "Large Cap",  "➡  Moderate (8-15% CAGR)", "Global payments network; cross-border"),
    ("PYPL", "PayPal Holdings",          "Technology",     "Digital Payments / Fintech",     "Large Cap",  "➡  Moderate (8-15% CAGR)", "Online payments + Venmo + Braintree"),
    ("SQ",   "Block Inc",                "Technology",     "Digital Payments / Fintech",     "Mid Cap",    "➡  Moderate (8-15% CAGR)", "Cash App + Square POS; Bitcoin exposure"),
    ("AFRM", "Affirm Holdings",          "Technology",     "Digital Payments / Fintech",     "Mid Cap",    "➡  Moderate (8-15% CAGR)", "BNPL lending; Apple Pay Later partner"),
    ("SOFI", "SoFi Technologies",        "Technology",     "Digital Payments / Fintech",     "Mid Cap",    "➡  Moderate (8-15% CAGR)", "Digital bank + lending + investing"),
    ("COIN", "Coinbase Global",          "Technology",     "Digital Payments / Fintech",     "Mid Cap",    "➡  Moderate (8-15% CAGR)", "Crypto exchange; institutional custody"),
    ("HOOD", "Robinhood Markets",        "Technology",     "Digital Payments / Fintech",     "Mid Cap",    "➡  Moderate (8-15% CAGR)", "Retail brokerage + crypto; Gold subscription"),
    ("NU",   "Nu Holdings",              "Technology",     "Digital Payments / Fintech",     "Large Cap",  "➡  Moderate (8-15% CAGR)", "Largest neobank; 100M+ customers in LatAm"),
    ("FI",   "Fiserv Inc",               "Technology",     "Digital Payments / Fintech",     "Large Cap",  "➡  Moderate (8-15% CAGR)", "Payment processing + Clover POS"),
    ("FIS",  "Fidelity National Info Svcs","Technology",   "Digital Payments / Fintech",     "Large Cap",  "➡  Moderate (8-15% CAGR)", "Banking + payment technology"),
    ("AXP",  "American Express",         "Technology",     "Digital Payments / Fintech",     "Large Cap",  "➡  Moderate (8-15% CAGR)", "Premium card network; spend-centric model"),
    ("BILL", "BILL Holdings Inc",        "Technology",     "Digital Payments / Fintech",     "Mid Cap",    "➡  Moderate (8-15% CAGR)", "SMB AP/AR automation; AI-powered invoice processing"),
    ("FOUR", "Shift4 Payments Inc",      "Technology",     "Digital Payments / Fintech",     "Mid Cap",    "⬆  High (15-25% CAGR)",   "Full-stack payments for hospitality & stadiums; $30B+ payment volume"),

    # ═══════════════════════════════════════════════════════════════
    #  DIGITAL MEDIA / STREAMING / ADTECH (9)
    # ═══════════════════════════════════════════════════════════════
    ("NFLX", "Netflix Inc",              "Technology",     "Digital Media / Streaming / AdTech","Large Cap", "➡  Moderate (8-15% CAGR)", "Streaming leader; ad tier + live sports"),
    ("DIS",  "Walt Disney Co",           "Technology",     "Digital Media / Streaming / AdTech","Large Cap", "➡  Moderate (8-15% CAGR)", "Disney+ + ESPN + parks + studios"),
    ("SPOT", "Spotify Technology",       "Technology",     "Digital Media / Streaming / AdTech","Large Cap", "➡  Moderate (8-15% CAGR)", "Audio streaming + podcast + audiobooks"),
    ("ROKU", "Roku Inc",                 "Technology",     "Digital Media / Streaming / AdTech","Mid Cap",   "➡  Moderate (8-15% CAGR)", "CTV OS + OneView ad platform"),
    ("TTD",  "The Trade Desk",           "Technology",     "Digital Media / Streaming / AdTech","Large Cap", "➡  Moderate (8-15% CAGR)", "Programmatic ad buying; CTV tailwind"),
    ("APP",  "AppLovin Corp",            "Technology",     "Digital Media / Streaming / AdTech","Large Cap", "➡  Moderate (8-15% CAGR)", "Mobile ad tech; AXON AI engine"),
    ("MGNI", "Magnite Inc",              "Technology",     "Digital Media / Streaming / AdTech","Small Cap", "➡  Moderate (8-15% CAGR)", "CTV supply-side ad platform"),
    ("WBD",  "Warner Bros Discovery",   "Technology",     "Digital Media / Streaming / AdTech","Mid Cap",   "➡  Moderate (8-15% CAGR)", "Max streaming + CNN + DC; debt restructuring"),
    ("PARA", "Paramount Global",         "Technology",     "Digital Media / Streaming / AdTech","Mid Cap",   "➡  Moderate (8-15% CAGR)", "Paramount+ + CBS; potential M&A target"),

    # ═══════════════════════════════════════════════════════════════
    #  SiC / WIDE BANDGAP POWER SEMIS (4)
    #  Note: ON also appears in AI Semiconductors above
    # ═══════════════════════════════════════════════════════════════
    ("AEHR", "Aehr Test Systems",        "Technology",     "SiC / Wide Bandgap Power Semis", "Small Cap",  "⬆  High (15-25% CAGR)",   "SiC wafer burn-in test; pivoting to AI/HBM accelerator testing"),
    ("ACLS", "Axcelis Technologies",     "Technology",     "SiC / Wide Bandgap Power Semis", "Mid Cap",    "⬆  High (15-25% CAGR)",   "Ion implant equipment for SiC wafers; EV + power electronics"),
    ("WOLF", "Wolfspeed Inc",            "Technology",     "SiC / Wide Bandgap Power Semis", "Small Cap",  "⬆  High (15-25% CAGR)",   "SiC wafer & device manufacturer; restructuring + EV/data center demand"),

    # ═══════════════════════════════════════════════════════════════
    #  SEMICONDUCTOR TEST EQUIPMENT (3)
    # ═══════════════════════════════════════════════════════════════
    ("TER",  "Teradyne Inc",             "Technology",     "Semiconductor Test Equipment",   "Mid Cap",    "⬆  High (15-25% CAGR)",   "ATE for HBM/SoC + Universal Robots; AI test demand recovering"),
    ("COHU", "Cohu Inc",                 "Technology",     "Semiconductor Test Equipment",   "Small Cap",  "⬆  High (15-25% CAGR)",   "Semiconductor test handlers; SiC & automotive exposure"),
    ("FORM", "FormFactor Inc",           "Technology",     "Semiconductor Test Equipment",   "Small Cap",  "⬆  High (15-25% CAGR)",   "Probe cards for advanced wafer test; HBM & AI accelerator chips"),

    # ═══════════════════════════════════════════════════════════════
    #  AI DEFENSE & INTELLIGENCE (2)
    # ═══════════════════════════════════════════════════════════════
    ("CACI", "CACI International",       "Technology",     "AI Defense & Intelligence",      "Large Cap",  "⬆  High (15-25% CAGR)",   "AI-powered defense IT & intelligence analytics; $8B+ DoD backlog"),
    ("LHX",  "L3Harris Technologies",    "Technology",     "AI Defense & Intelligence",      "Large Cap",  "⬆  High (15-25% CAGR)",   "Defense electronics; signals intelligence; AI-enabled ISR"),
]

# ── Build ticker list (deduplicated, order-preserving) ─────────────────
HG:   list[str]       = list(dict.fromkeys(row[0] for row in HG_DATA))
META: dict[str, dict] = {}

for _row in HG_DATA:
    _t, _name, _sec, _sub, _tier, _gtier, _thesis = _row
    META[_t] = {
        "name":            _name,
        "sector":          _sec,
        "sub_sector":      _sub,
        "subsector":       _sub,
        "market_cap_tier": _tier,
        "growth_tier":     _gtier,
        "thesis":          _thesis,
        "rationale":       _thesis,
        "exchange":        _EXCHANGE.get(_t, "NASDAQ"),
    }


# ── Phase 2: Index constituent expansion ──────────────────────────────
# The curated HG universe (~187 tickers) is tech/telecom/infra heavy.
# To support Conservative / Balanced profiles drawn from S&P 500 /
# NASDAQ-100 / Dow 30, we extend the ingestion list with everyone in
# those indices who isn't already in HG. Metadata for these extras is
# minimal — the pipeline fetches real name/sector/exchange from FMP/YF
# at refresh time, so all we need is the symbol.
try:
    from index_constituents import SP500, NASDAQ_100, DOW_30
    _INDEX_UNIVERSE: set[str] = set(SP500) | set(NASDAQ_100) | set(DOW_30)
except Exception:
    _INDEX_UNIVERSE = set()

# Tickers in the major indices that aren't already in our curated HG list.
INDEX_EXTRA: list[str] = sorted(_INDEX_UNIVERSE - set(HG))


# ── Phase 3: the full listed universe ─────────────────────────────────
# data/universe_index.json is a static snapshot of every S&P 500 / MidCap
# 400 / SmallCap 600 / Nasdaq-100 / Dow constituent with its name, GICS
# sector and index tags — about 1,500 names. The app only ever reads it, so
# carrying the long tail costs nothing at runtime.
#
# THE $1B FLOOR: S&P index eligibility already enforces a float-adjusted
# market-cap minimum around $1B (SmallCap 600 sits roughly $1.1bn-$7.4bn),
# so membership IS the floor. `full_universe(min_cap=..., caps=...)` applies
# a live numeric floor on top when the caller has market caps to hand,
# because that figure is accurate and free once a name has been enriched.
import json as _json
import os as _os

_FULL_META: dict[str, dict] = {}
try:
    with open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                            "data", "universe_index.json"), encoding="utf-8") as _fh:
        _FULL_META = _json.load(_fh)
except Exception:
    _FULL_META = {}

FULL: list[str] = sorted(_FULL_META)
#: Names in the full listed universe the curated list does not already cover.
FULL_EXTRA: list[str] = sorted(set(FULL) - set(HG))


def indices_for(ticker: str) -> list[str]:
    """Index tags for a ticker — SPX / SP400 / SP600 / NDX / DJI."""
    return list((_FULL_META.get((ticker or "").upper()) or {}).get("indices") or [])


def full_universe(min_cap: float | None = None, caps: dict | None = None) -> list[str]:
    """The full listed universe. Pass `caps` ({ticker: market_cap}) to apply a
    live market-cap floor; without it, index membership is the floor."""
    if not min_cap or not caps:
        return FULL
    return [t for t in FULL
            if caps.get(t) is None or float(caps.get(t) or 0) >= min_cap]


def _placeholder_meta(ticker: str) -> dict:
    """Lightweight meta for index-extra tickers — the FMP/YF enrichment
    overwrites these on the first refresh cycle. Empty strings are used
    for sector / sub_sector / tier so the merge logic in data_coordinator
    falls THROUGH to live data instead of treating placeholder em-dashes
    as truthy values (which previously left ~7% of the universe stuck on
    '—' when YF returned partial data without sector)."""
    # The snapshot carries a real company name, GICS sector and index tags for
    # the long tail, so these are no longer blank placeholders — but sector is
    # still left empty when unknown so the merge chain falls through to live
    # data rather than treating a placeholder as truthy.
    fm = _FULL_META.get(ticker) or {}
    idx = fm.get("indices") or []
    return {
        "name":            fm.get("name") or ticker,
        "sector":          fm.get("sector") or "",
        "sub_sector":      "",
        "subsector":       "",
        "market_cap_tier": "",
        "growth_tier":     "",
        "indices":         idx,
        "thesis":          (f"Constituent of {', '.join(idx)}." if idx
                            else "Index constituent"),
        "rationale":       (f"Constituent of {', '.join(idx)}." if idx
                            else "Index constituent"),
        "exchange":        _EXCHANGE.get(ticker, "NASDAQ"),
    }


# ── Public API ────────────────────────────────────────────────────────

def get_universe(mode: str = "hg") -> list[str]:
    if mode == "fast":     return FAST
    if mode == "indices":  return sorted(_INDEX_UNIVERSE)           # only the index list
    if mode == "expanded": return HG + INDEX_EXTRA                  # curated + index extras
    # "full" — the whole listed universe (~1,500). Curated names come first so
    # anything that reads the head of the list still gets the enriched ones.
    if mode == "full":     return HG + FULL_EXTRA
    return HG   # "hg" / "all" — curated universe (unchanged default)


def get_meta(ticker: str) -> dict:
    t = (ticker or "").upper()
    if t in META:
        return META[t]
    if t in _FULL_META or t in _INDEX_UNIVERSE:
        return _placeholder_meta(t)
    return {}
