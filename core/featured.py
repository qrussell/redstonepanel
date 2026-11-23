"""The catalog's starter set: popular server-side mods and plugins, picked by hand, and a few
bundles of them. Only Modrinth project slugs are listed here; titles, icons, downloads and which
loaders and Minecraft versions a project supports come from Modrinth when the catalog opens.

Rules for adding one: it runs on a server (server_side required or optional), it's actively
maintained, and it's widely used. Paper has spark built in, so spark is Fabric-only here.
"""

# kind -> [(group id, group title, [slugs])]
GROUPS = {
    "fabric": [
        ("performance", "Performance", ["lithium", "ferrite-core", "krypton", "c2me-fabric", "scalablelux",
                                        "modernfix"]),
        ("admin", "Admin tools", ["spark", "chunky", "luckperms", "worldedit", "ledger", "essential-commands"]),
        ("players", "For players", ["simple-voice-chat", "bluemap", "geyser", "floodgate", "viaversion",
                                    "universal-graves", "no-chat-reports"]),
        ("library", "Libraries", ["fabric-api"]),
    ],
    "paper": [
        ("performance", "Performance", ["chunky"]),
        ("admin", "Admin tools", ["luckperms", "essentialsx", "worldedit", "worldguard", "coreprotect",
                                  "griefprevention", "multiverse-core"]),
        ("players", "For players", ["simple-voice-chat", "bluemap", "geyser", "viaversion", "viabackwards"]),
        ("library", "Libraries", ["placeholderapi"]),
    ],
}

# kind -> [(bundle id, title, description, [slugs])]
BUNDLES = {
    "fabric": [
        ("performance", "Performance pack",
         "Faster ticks, less memory and quicker chunk loading, with no gameplay changes.",
         ["lithium", "ferrite-core", "krypton", "c2me-fabric", "scalablelux"]),
        ("essentials", "Server essentials",
         "Profiling (spark), world pre-generation (Chunky), permissions (LuckPerms) and a block log (Ledger).",
         ["spark", "chunky", "luckperms", "ledger"]),
        ("bedrock", "Bedrock players",
         "Lets Bedrock players (consoles, phones) join this Java server, without a Java account.",
         ["geyser", "floodgate"]),
    ],
    "paper": [
        ("essentials", "Server essentials",
         "World pre-generation (Chunky), permissions (LuckPerms), everyday commands (EssentialsX) and "
         "a block log with rollback (CoreProtect). Paper already includes spark.",
         ["chunky", "luckperms", "essentialsx", "coreprotect"]),
        ("protection", "Build protection",
         "Protected regions (WorldGuard, with WorldEdit) and a block log with rollback (CoreProtect).",
         ["worldedit", "worldguard", "coreprotect"]),
        ("bedrock", "Bedrock players",
         "Lets Bedrock players (consoles, phones) join this Java server. Players need a Java account "
         "unless you add Floodgate from geysermc.org.",
         ["geyser"]),
    ],
}

# 1.20: Quilt loads Fabric mods and Purpur loads Paper plugins, so they share those sets (a
# card says so when a project has no build for them). Forge and NeoForge have their own.
GROUPS["quilt"] = GROUPS["fabric"]
BUNDLES["quilt"] = BUNDLES["fabric"]
GROUPS["purpur"] = GROUPS["paper"]
BUNDLES["purpur"] = BUNDLES["paper"]
GROUPS["forge"] = [
    ("performance", "Performance", ["ferrite-core", "modernfix", "servercore", "clumps"]),
    ("admin", "Admin tools", ["spark", "chunky", "luckperms", "worldedit", "open-parties-and-claims"]),
    ("players", "For players", ["simple-voice-chat", "bluemap", "corpse", "no-chat-reports", "jei"]),
    ("library", "Libraries", ["architectury-api", "cloth-config"]),
]
GROUPS["neoforge"] = [
    ("performance", "Performance", ["lithium", "ferrite-core", "modernfix", "scalablelux", "servercore", "clumps"]),
    ("admin", "Admin tools", ["spark", "chunky", "luckperms", "worldedit", "open-parties-and-claims"]),
    ("players", "For players", ["simple-voice-chat", "bluemap", "geyser", "floodgate", "corpse",
                                "no-chat-reports", "jei"]),
    ("library", "Libraries", ["architectury-api", "cloth-config"]),
]
BUNDLES["forge"] = [
    ("performance", "Performance pack",
     "Less memory and fewer wasted ticks, with no gameplay changes.",
     ["ferrite-core", "modernfix", "servercore"]),
    ("essentials", "Server essentials",
     "Profiling (spark), world pre-generation (Chunky) and permissions (LuckPerms).",
     ["spark", "chunky", "luckperms"]),
]
BUNDLES["neoforge"] = [
    ("performance", "Performance pack",
     "Faster ticks, less memory and quicker lighting, with no gameplay changes.",
     ["lithium", "ferrite-core", "modernfix", "scalablelux"]),
    ("essentials", "Server essentials",
     "Profiling (spark), world pre-generation (Chunky) and permissions (LuckPerms).",
     ["spark", "chunky", "luckperms"]),
    ("bedrock", "Bedrock players",
     "Lets Bedrock players (consoles, phones) join this Java server, without a Java account.",
     ["geyser", "floodgate"]),
]

# Categories each source can filter by: (id, label)
CATEGORIES = {
    "modrinth": [("optimization", "Optimization"), ("management", "Management"), ("utility", "Utility"),
                 ("worldgen", "World generation"), ("economy", "Economy"), ("social", "Social"),
                 ("game-mechanics", "Game mechanics"), ("adventure", "Adventure"), ("decoration", "Decoration"),
                 ("storage", "Storage"), ("transportation", "Transportation"), ("technology", "Technology"),
                 ("equipment", "Equipment"), ("mobs", "Mobs"), ("magic", "Magic"), ("minigame", "Minigames"),
                 ("library", "Libraries")],
    "hangar": [("admin_tools", "Admin tools"), ("chat", "Chat"), ("economy", "Economy"),
               ("gameplay", "Gameplay"), ("games", "Games"), ("protection", "Protection"),
               ("role_playing", "Role playing"), ("world_management", "World management"),
               ("dev_tools", "Developer tools"), ("misc", "Other")],
}

# Sort orders offered for every source: (id, label)
SORTS = [("downloads", "Most downloaded"), ("follows", "Most followed"), ("updated", "Recently updated"),
         ("newest", "Newest")]


def slugs(kind):
    out = []
    for _, _, items in GROUPS.get(kind, []):
        out += items
    for _, _, _, items in BUNDLES.get(kind, []):
        out += items
    return sorted(set(out))
