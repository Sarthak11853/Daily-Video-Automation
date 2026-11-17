"""The visual vocabulary shared by the script writer (LLM prompt), the
validator and both renderers. The LLM may only pick from these lists, which
guarantees every scene can be built from reusable, license-free procedural
3D assets and keeps the channel's look consistent."""

ENVIRONMENTS = {
    "space": "deep-space backdrop with stars and distant planets",
    "ocean": "underwater scene with sand, seaweed, coral and bubbles",
    "lab": "bright cartoon science lab with beakers and shelves",
    "city": "colourful toy-like city with blocky buildings",
    "ancient": "desert with pyramids and stone columns (history)",
    "jungle": "lush jungle with round trees, bushes and mountains",
    "arctic": "icy landscape with snow, ice blocks and cold sky",
    "mind": "purple dream-space with floating glowing neurons (psychology)",
    "earth": "planet Earth globe floating against a blue sky (geography)",
    "mystery": "dark misty night with moon and floating question marks",
    "desert": "sunny sand dunes with cacti and rocks",
    "studio": "clean gradient studio stage with a podium (intros/outros)",
}

PROPS = {
    "planet": "ringed cartoon planet", "moon": "cratered moon", "sun": "glowing sun", "star": "big glowing star",
    "rocket": "cartoon rocket", "satellite": "satellite with solar panels", "telescope": "telescope",
    "atom": "atom with orbiting electrons", "dna": "DNA double helix", "brain": "cartoon brain",
    "heart": "cartoon heart", "globe": "Earth globe", "pyramid": "pyramid", "column": "Greek column",
    "tree": "round cartoon tree", "mountain": "snow-capped mountain", "volcano": "volcano with glowing lava",
    "fish": "cartoon fish", "octopus": "cartoon octopus", "bird": "cartoon bird", "creature": "cute generic animal",
    "book": "open book", "lightbulb": "glowing light bulb", "question": "3D question mark",
    "exclamation": "3D exclamation mark", "magnifier": "magnifying glass", "clock": "clock",
    "crystal": "glowing crystal", "beaker": "lab beaker with glowing liquid", "coin": "gold coin",
    "skull": "cartoon skull (ancient/mystery only)", "bone": "fossil bone", "chart": "rising bar chart",
    "robot": "small robot", "ice": "ice block", "drop": "water drop", "lightning": "lightning bolt",
    "eye": "cartoon eye", "map": "folded map", "chip": "computer chip", "phone": "smartphone",
}

MASCOT_ACTIONS = {
    "idle": "gentle bobbing", "wave": "waves hello", "surprised": "jumps back, eyes wide",
    "point": "points at the hero object", "think": "tilts head thinking", "celebrate": "jumps and spins",
    "explain": "talks with arm gestures", "scared": "shivers", "none": "mascot hidden",
}

CAMERA_MOVES = {
    "push_in": "slow dolly towards subject", "pull_out": "reveal by pulling back",
    "orbit_left": "orbit around subject to the left", "orbit_right": "orbit to the right",
    "pan_up": "tilt/pan upwards", "pan_down": "tilt/pan downwards", "dolly_side": "slide sideways",
    "crane_down": "descend from above", "static": "almost still, subtle breathing motion",
}

SFX = ["none", "whoosh", "pop", "ding", "boom", "sparkle", "swoosh_up"]


def closest(value: str | None, options: dict | list, default: str) -> str:
    """Map an unknown LLM choice onto the vocabulary (exact > prefix > substring > default)."""
    keys = list(options)
    v = (value or "").strip().lower().replace(" ", "_").replace("-", "_")
    if v in keys:
        return v
    for k in keys:
        if v.startswith(k) or k.startswith(v) and v:
            return k
    for k in keys:
        if v and (k in v or v in k):
            return k
    return default
