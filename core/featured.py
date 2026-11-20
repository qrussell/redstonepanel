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
