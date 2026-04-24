
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from collections import deque, defaultdict
import json
import html
import math
import os
import re
import socket
import time

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 8000))

W = 6
H = 8
TOTAL_CELLS = W * H
TARGET_EMPTY = 4

GOAL_BASE_TYPE = 7
GOAL_TYPE = 70
GOAL_DISPLAY_ID = 9

LEVELS_FILE = "levels.json"
PROGRESS_FILE = "papa_puzzle_progress.json"

TOTAL_LEVELS = 300
# Pavages complets du 6x8 : enorme (>> 10^4 par inventaire). Le jeu ne voit que les etats
# ACCESSIBLES PAR LES COUPS depuis un etat but ; un seul but ne couvrait qu'une minuscule fraction.
# On tire plusieurs pavages (buts) par inventaire pour reunir assez de grilles uniques sans doublons.
GOAL_STATES_PER_MASK_CAP = 80
GOAL_STATES_TOTAL_CAP_PER_INVENTORY = 250
VISIBLE_START = 20
VISIBLE_AFTER_20 = 50
VISIBLE_AFTER_50 = 100
VISIBLE_AFTER_100 = 300

# Feux d'artifice apres avoir termine le niveau 20, 50, 100 ou 300 (palier debloque).
CELEBRATE_MILESTONES = frozenset({20, 50, 100, 300})

WELCOME_MESSAGE = "Cher Papa, je t'aime de tout mon coeur."

def min_moves_required_for_level(level_id):
    """Coups minimum (vers le 9 en bas) pour le palier : niveau 1 = tutoriel a 3 coups ;
    a partir du niveau 2 : 5 + floor(id/20)."""
    if level_id < 1:
        return 3
    if level_id == 1:
        return 3
    return 5 + (level_id // 20)


def branch_bounds_for_level(level_id):
    """Fenetre [min,max] sur les possibilites immediates au depart.
    Cible utilisateur 10..45 + id//25 ; le plateau n'a au plus que 7 branches — on projette sur 1..7."""
    u_lo = 10 + (level_id // 25)
    u_hi = 45 + (level_id // 25)
    span = 47.0
    t1 = (u_lo - 10) / span
    t2 = (u_hi - 10) / span
    b_lo = max(1, min(7, int(round(1 + t1 * 6))))
    b_hi = max(1, min(7, int(round(1 + t2 * 6))))
    if b_lo > b_hi:
        b_lo, b_hi = b_hi, b_lo
    return b_lo, b_hi


def min_branch_for_level(level_id):
    return branch_bounds_for_level(level_id)[0]


def max_branch_for_level(level_id):
    return branch_bounds_for_level(level_id)[1]


def min_points_required_for_level(level_id):
    """Score de difficulte minimum : +30 tous les 10 niveaux (130 cible reduit par +100 sur les possibilites)."""
    return 30 * (level_id // 10)
# None = exploration BFS complète depuis le but (tous les états atteignables).
# Un entier positif tronque (utile seulement pour tests rapides).
MAX_BFS_STATES_PER_INVENTORY = None
# Messages intermédiaires pendant le BFS (baisser pour plus de lignes dans le terminal).
BFS_PROGRESS_EVERY = 10_000
# Incrémenter pour invalider un vieux levels.json et forcer une nouvelle génération.
LEVELS_GENERATOR_VERSION = 17

PIECE_COLORS = {
    0: "#eee8dd",
    1: "#f2c66d",
    2: "#79bdd9",
    3: "#88c977",
    4: "#b58ee7",
    5: "#ef9696",
    6: "#f4b264",
    7: "#6eb0f5",
    9: "#5b8def",
}

PIECE_NAMES = {
    0: "Vide",
    1: "1x1",
    2: "2x1",
    3: "L",
    4: "3x1",
    5: "2x2",
    6: "Z",
    7: "4x1",
    9: "But",
}

# Par inventaire : (1x1, 2x1, L, 3x1, 2x2, Z, 4x1).
# Stock autorise : 1x1 max 5, 2x1 max 2, L max 2, 3x1 max 3, 2x2 max 3, Z max 2, 4x1 = 1.
# Sous ces bornes, seule une combinaison a moins de 3 x 2x2 : (5,2,2,3,2,2,1) avec 2 x 2x2.
# Les 4 autres doivent avoir 3 x 2x2 pour atteindre une aire totale de 44.
TOTAL_INVENTORIES = [
    (5, 2, 2, 3, 2, 2, 1),
    (1, 2, 2, 3, 3, 2, 1),
    (3, 1, 2, 3, 3, 2, 1),
    (4, 2, 2, 2, 3, 2, 1),
    (5, 2, 2, 3, 3, 1, 1),
]

BASE_PIECES = {
    1: [(0, 0)],
    2: [(0, 0), (1, 0)],
    3: [(0, 0), (1, 0), (1, 1)],
    4: [(0, 0), (1, 0), (2, 0)],
    5: [(0, 0), (1, 0), (0, 1), (1, 1)],
    6: [(0, 0), (1, 0), (1, 1), (2, 1)],
    7: [(0, 0), (1, 0), (2, 0), (3, 0)],
}

FILL_TYPES = [1, 2, 3, 4, 5, 6, 7]
TYPE_TO_BASE = {pid: pid for pid in FILL_TYPES}
TYPE_TO_BASE[GOAL_TYPE] = GOAL_BASE_TYPE
AREA_BY_TYPE = {
    pid: len(BASE_PIECES[TYPE_TO_BASE[pid]])
    for pid in list(FILL_TYPES) + [GOAL_TYPE]
}

def popcount(n):
    return bin(n).count("1")

def total_inventory_area(inv):
    c1, c2, c3, c4, c5, c6, c7 = inv
    return c1 * 1 + c2 * 2 + c3 * 3 + c4 * 3 + c5 * 4 + c6 * 4 + c7 * 4

def validate_inventories():
    bad = []
    maxima = [0] * 7
    z2 = 0
    missing = 0
    for i, inv in enumerate(TOTAL_INVENTORIES, start=1):
        if total_inventory_area(inv) != 44:
            bad.append((i, inv, total_inventory_area(inv)))
        if inv[6] != 1:
            bad.append((i, inv, "le nombre total de 4x1 doit etre 1"))
        if inv[5] == 2:
            z2 += 1
        if 0 in inv[:6]:
            missing += 1
        for j in range(7):
            maxima[j] = max(maxima[j], inv[j])
    weighted = (
        maxima[0] * 1 + maxima[1] * 2 + maxima[2] * 3 + maxima[3] * 3 +
        maxima[4] * 4 + maxima[5] * 4 + maxima[6] * 4
    )
    if weighted > 52:
        bad.append(("global", tuple(maxima), f"somme ponderee = {weighted} > 52"))
    if z2 < 1:
        bad.append(("global", "aucun inventaire avec 2 Z"))
    if not (0 <= missing <= 4):
        bad.append(("global", f"il faut 0 à 4 inventaires avec un type absent, trouve {missing}"))
    if bad:
        raise ValueError(f"Inventaires invalides: {bad}")

validate_inventories()

def convert_total_inventory_to_fill_inventory(inv):
    c1, c2, c3, c4, c5, c6, c7 = inv
    return (c1, c2, c3, c4, c5, c6, c7 - 1)

def normalize_shape(cells):
    min_x = min(x for x, y in cells)
    min_y = min(y for x, y in cells)
    return tuple(sorted((x - min_x, y - min_y) for x, y in cells))

def rotate_shape(cells):
    return normalize_shape([(-y, x) for x, y in cells])

def all_rotations(cells):
    result = set()
    cur = normalize_shape(cells)
    for _ in range(4):
        result.add(cur)
        cur = rotate_shape(cur)
    return sorted(result)

PIECE_ROTATIONS = {pid: all_rotations(shape) for pid, shape in BASE_PIECES.items()}

def xy_to_idx(x, y):
    return y * W + x

def idx_to_xy(i):
    return (i % W, i // W)

def cells_to_mask(cells):
    mask = 0
    for x, y in cells:
        mask |= (1 << xy_to_idx(x, y))
    return mask

def mask_indices(mask):
    while mask:
        lsb = mask & -mask
        yield lsb.bit_length() - 1
        mask ^= lsb

def mask_to_cells(mask):
    return [idx_to_xy(i) for i in mask_indices(mask)]

def mirror_mask(mask):
    out = 0
    for i in mask_indices(mask):
        x, y = idx_to_xy(i)
        mx = W - 1 - x
        out |= (1 << xy_to_idx(mx, y))
    return out

def normalize_state(pieces):
    return tuple(sorted(pieces))

def state_to_occupied_mask(state):
    occ = 0
    for _, mask in state:
        occ |= mask
    return occ

def mirror_state(state):
    return normalize_state((pid, mirror_mask(mask)) for pid, mask in state)

def canonical_mirror_state(state):
    mirrored = mirror_state(state)
    return min(state, mirrored)

def display_value(pid):
    return GOAL_DISPLAY_ID if pid == GOAL_TYPE else pid

def state_to_board(state):
    board = [[0 for _ in range(W)] for _ in range(H)]
    for pid, mask in state:
        val = display_value(pid)
        for i in mask_indices(mask):
            x, y = idx_to_xy(i)
            board[y][x] = val
    return board


def state_to_piece_ids_board(state):
    """Une id distincte par piece physique (pour tracer les bords au rendu)."""
    out = [[0 for _ in range(W)] for _ in range(H)]
    for piece_idx, (_, mask) in enumerate(state, start=1):
        for i in mask_indices(mask):
            x, y = idx_to_xy(i)
            out[y][x] = piece_idx
    return out


def infer_piece_ids_from_board(board):
    """Si piece_ids absent du JSON : regroupe par case adjacentes de meme chiffre (approximation)."""
    pids = [[0 for _ in range(W)] for _ in range(H)]
    cur = 0
    for y in range(H):
        for x in range(W):
            if board[y][x] == 0 or pids[y][x]:
                continue
            cur += 1
            val = board[y][x]
            stack = [(x, y)]
            while stack:
                cx, cy = stack.pop()
                if cx < 0 or cx >= W or cy < 0 or cy >= H:
                    continue
                if pids[cy][cx] or board[cy][cx] != val:
                    continue
                pids[cy][cx] = cur
                stack.extend([(cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)])
    return pids

def piece_count(state):
    return len(state)

def build_placements_for_type(pid):
    seen = set()
    placements = []
    base_pid = TYPE_TO_BASE[pid]
    for shape in PIECE_ROTATIONS[base_pid]:
        max_x = max(x for x, y in shape)
        max_y = max(y for x, y in shape)
        for oy in range(H - max_y):
            for ox in range(W - max_x):
                mask = cells_to_mask((ox + dx, oy + dy) for dx, dy in shape)
                if mask not in seen:
                    seen.add(mask)
                    placements.append(mask)
    placements.sort()
    return placements

PLACEMENTS_BY_TYPE = {
    pid: build_placements_for_type(pid)
    for pid in list(FILL_TYPES) + [GOAL_TYPE]
}

def goal_mask_is_in_bottom_left(mask):
    cells = mask_to_cells(mask)
    cell_set = set(cells)
    return (
        (0, H - 1) in cell_set and
        min(x for x, y in cells) == 0 and
        max(y for x, y in cells) == H - 1
    )

GOAL_MASKS = [mask for mask in PLACEMENTS_BY_TYPE[GOAL_TYPE] if goal_mask_is_in_bottom_left(mask)]

def is_winning_state(state):
    """Victoire : le bloc But (affichage 9) est en position bas-gauche valide, pas toute la grille."""
    for pid, mask in state:
        if pid == GOAL_TYPE and goal_mask_is_in_bottom_left(mask):
            return True
    return False

PLACEMENTS_COVERING_CELL = defaultdict(list)
for pid in sorted(FILL_TYPES, key=lambda p: (-AREA_BY_TYPE[p], p)):
    for mask in PLACEMENTS_BY_TYPE[pid]:
        for cell_idx in mask_indices(mask):
            PLACEMENTS_COVERING_CELL[cell_idx].append((pid, mask))
for cell_idx in PLACEMENTS_COVERING_CELL:
    PLACEMENTS_COVERING_CELL[cell_idx].sort(
        key=lambda item: (-AREA_BY_TYPE[item[0]], item[0], item[1])
    )

def first_uncovered_cell_not_marked(covered_mask, void_mask):
    used = covered_mask | void_mask
    for i in range(TOTAL_CELLS):
        if ((used >> i) & 1) == 0:
            return i
    return None

def collect_goal_states_for_inventory(
    total_inv,
    per_mask_limit=None,
    total_limit=None,
):
    """Enumere des pavages complets (etat but) pour un stock de pieces donne.
    `per_mask_limit` / `total_limit` par defaut = constantes globales (plusieurs milliers de solutions possibles)."""
    if per_mask_limit is None:
        per_mask_limit = GOAL_STATES_PER_MASK_CAP
    if total_limit is None:
        total_limit = GOAL_STATES_TOTAL_CAP_PER_INVENTORY
    fill_inv = convert_total_inventory_to_fill_inventory(total_inv)
    stock_init = {1: fill_inv[0], 2: fill_inv[1], 3: fill_inv[2], 4: fill_inv[3], 5: fill_inv[4], 6: fill_inv[5], 7: fill_inv[6]}
    out = []
    seen_global = set()

    for goal_mask in GOAL_MASKS:
        collected_this_mask = 0

        def backtrack(covered_mask, void_mask, empties_left, stock_left, pieces):
            nonlocal collected_this_mask
            if len(out) >= total_limit:
                return
            if collected_this_mask >= per_mask_limit:
                return
            used_mask = covered_mask | void_mask
            remaining_cells = TOTAL_CELLS - popcount(used_mask)
            if remaining_cells < empties_left:
                return
            remaining_fill_area = sum(stock_left[pid] * AREA_BY_TYPE[pid] for pid in stock_left)
            if remaining_fill_area != remaining_cells - empties_left:
                return
            if remaining_cells == 0:
                if empties_left == 0 and all(stock_left[pid] == 0 for pid in stock_left):
                    ns = normalize_state(pieces)
                    if ns not in seen_global:
                        seen_global.add(ns)
                        out.append(ns)
                        collected_this_mask += 1
                return
            cell = first_uncovered_cell_not_marked(covered_mask, void_mask)
            if cell is None:
                return
            if empties_left > 0:
                backtrack(covered_mask, void_mask | (1 << cell), empties_left - 1, stock_left, pieces)
                if len(out) >= total_limit or collected_this_mask >= per_mask_limit:
                    return
            for pid, mask in PLACEMENTS_COVERING_CELL[cell]:
                if stock_left.get(pid, 0) <= 0:
                    continue
                if used_mask & mask:
                    continue
                new_stock = dict(stock_left)
                new_stock[pid] -= 1
                backtrack(covered_mask | mask, void_mask, empties_left, new_stock, pieces + ((pid, mask),))
                if len(out) >= total_limit or collected_this_mask >= per_mask_limit:
                    return

        start_state = ((GOAL_TYPE, goal_mask),)
        backtrack(goal_mask, 0, TARGET_EMPTY, stock_init, start_state)
        if len(out) >= total_limit:
            break
    return out


def find_one_goal_state_for_inventory(total_inv):
    goals = collect_goal_states_for_inventory(total_inv, per_mask_limit=1, total_limit=1)
    if not goals:
        raise RuntimeError(f"Aucun etat but trouve pour l'inventaire {total_inv}")
    return goals[0]

DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1))

def translate_mask(mask, dx, dy):
    out = 0
    for i in mask_indices(mask):
        x, y = idx_to_xy(i)
        nx = x + dx
        ny = y + dy
        if not (0 <= nx < W and 0 <= ny < H):
            return None
        out |= (1 << xy_to_idx(nx, ny))
    return out


def parallel_one_step(state, dx, dy):
    """Un pas simultane : chaque piece avance d'une case dans (dx,dy) si libre ; conflits annules."""
    pieces = list(state)
    occ = state_to_occupied_mask(state)
    n = len(pieces)
    proposed = []
    for i, (pid, mask) in enumerate(pieces):
        nxt = translate_mask(mask, dx, dy)
        if nxt is None:
            proposed.append(None)
            continue
        other = occ ^ mask
        if nxt & other:
            proposed.append(None)
        else:
            proposed.append(nxt)
    bad = [False] * n
    for i in range(n):
        if proposed[i] is None:
            continue
        for j in range(i + 1, n):
            if proposed[j] is None:
                continue
            if proposed[i] & proposed[j]:
                bad[i] = True
                bad[j] = True
    new_pieces = []
    for i, (pid, mask) in enumerate(pieces):
        if proposed[i] is not None and not bad[i]:
            new_pieces.append((pid, proposed[i]))
        else:
            new_pieces.append((pid, mask))
    return normalize_state(tuple(new_pieces))


def parallel_slide_direction(state, dx, dy):
    """Un coup utilisateur : toutes les pieces glissent en parallele dans (dx,dy), micro-pas repetes jusqu'a blocage."""
    cur = state
    for _ in range(TOTAL_CELLS * 4):
        nxt = parallel_one_step(cur, dx, dy)
        if nxt == cur:
            break
        cur = nxt
    return cur


def next_states(state):
    """Un coup = choisir une direction ; toutes les pieces bougent ensemble (parallele) jusqu'a arret."""
    result = []
    seen = set()
    for dx, dy in DIRS:
        nxt = parallel_slide_direction(state, dx, dy)
        if nxt == state:
            continue
        if nxt not in seen:
            seen.add(nxt)
            result.append((0, dx, dy, nxt))
    return result

def branching_factor(state):
    return len(next_states(state))

def reverse_bfs_from_goal(goal_state, max_states=None, progress_label="BFS", silent=False):
    """Parcours en largeur depuis l'état but : distance = nombre minimal de coups pour résoudre."""
    dist = {goal_state: 0}
    q = deque([goal_state])
    t0 = time.perf_counter()
    if not silent:
        print(
            f"  [{progress_label}] demarrage (rapport tous les {BFS_PROGRESS_EVERY} etats)...",
            flush=True,
        )
    while q:
        cur = q.popleft()
        for _, _, _, nxt in next_states(cur):
            if nxt not in dist:
                dist[nxt] = dist[cur] + 1
                q.append(nxt)
                if max_states is not None and len(dist) >= max_states:
                    if not silent:
                        dt = time.perf_counter() - t0
                        print(
                            f"  [{progress_label}] arret anticipe apres {len(dist)} etats ({dt:.1f}s)",
                            flush=True,
                        )
                    return dist
                if (
                    not silent
                    and max_states is None
                    and BFS_PROGRESS_EVERY > 0
                    and len(dist) % BFS_PROGRESS_EVERY == 0
                ):
                    dt = time.perf_counter() - t0
                    print(
                        f"  [{progress_label}] etats explores: {len(dist)} ({dt:.1f}s)...",
                        flush=True,
                    )
    if not silent:
        dt = time.perf_counter() - t0
        print(
            f"  [{progress_label}] BFS terminee: {len(dist)} etats atteignables ({dt:.1f}s)",
            flush=True,
        )
    return dist

def reverse_bfs_from_winning_set(winning_states, max_states=None, progress_label="BFS", silent=False):
    """Distance = nombre minimal de coups pour atteindre un etat ou le 9 est en bas a gauche."""
    dist = {}
    q = deque()
    for s in winning_states:
        dist[s] = 0
        q.append(s)
    t0 = time.perf_counter()
    if not silent:
        print(
            f"  [{progress_label}] demarrage (distance vers le 9 en bas, rapport tous les {BFS_PROGRESS_EVERY} etats)...",
            flush=True,
        )
    while q:
        cur = q.popleft()
        for _, _, _, nxt in next_states(cur):
            if nxt not in dist:
                dist[nxt] = dist[cur] + 1
                q.append(nxt)
                if max_states is not None and len(dist) >= max_states:
                    if not silent:
                        dt = time.perf_counter() - t0
                        print(
                            f"  [{progress_label}] arret anticipe apres {len(dist)} etats ({dt:.1f}s)",
                            flush=True,
                        )
                    return dist
                if (
                    not silent
                    and max_states is None
                    and BFS_PROGRESS_EVERY > 0
                    and len(dist) % BFS_PROGRESS_EVERY == 0
                ):
                    dt = time.perf_counter() - t0
                    print(
                        f"  [{progress_label}] etats explores: {len(dist)} ({dt:.1f}s)...",
                        flush=True,
                    )
    if not silent:
        dt = time.perf_counter() - t0
        print(
            f"  [{progress_label}] BFS terminee: {len(dist)} etats ({dt:.1f}s)",
            flush=True,
        )
    return dist

def distance_map_to_nearest_9_bottom(goal_state, progress_label, silent=False):
    """Parcourt le composant, puis calcule la distance vers l'ensemble des positions gagnantes (9 en bas)."""
    comp = reverse_bfs_from_goal(
        goal_state,
        MAX_BFS_STATES_PER_INVENTORY,
        progress_label=progress_label + " [composante]",
        silent=silent,
    )
    winning = [s for s in comp if is_winning_state(s)]
    if not winning:
        raise RuntimeError("Aucun etat gagnant (9 en bas) dans le composant — impossible.")
    return reverse_bfs_from_winning_set(
        winning,
        MAX_BFS_STATES_PER_INVENTORY,
        progress_label=progress_label + " [9 en bas]",
        silent=silent,
    )

def difficulty_score(state):
    return 48 - piece_count(state)

def merge_candidate_key(item):
    state, d = item
    return (d, -branching_factor(state), difficulty_score(state))


def shortest_path_to_nearest_win(start):
    """Chemin le plus court (en coups) jusqu'a un etat ou le 9 est en position but."""
    if is_winning_state(start):
        return [start]
    parent = {start: None}
    q = deque([start])
    goal = None
    while q:
        cur = q.popleft()
        if is_winning_state(cur):
            goal = cur
            break
        for *_, nxt in next_states(cur):
            if nxt not in parent:
                parent[nxt] = cur
                q.append(nxt)
    if goal is None:
        return None
    path = []
    cur = goal
    while cur is not None:
        path.append(cur)
        cur = parent.get(cur)
    path.reverse()
    return path


def compute_position_metrics(state):
    """Points = 100*coups + 5*somme(branching sur le chemin) + somme(log(branching)) (produit en log)."""
    path = shortest_path_to_nearest_win(state)
    if path is None:
        return None
    moves = len(path) - 1
    br_start = branching_factor(state)
    if moves == 0:
        b0 = max(1, br_start)
        sum_path = b0
        log_prod = math.log(b0)
        points = 5.0 * sum_path + log_prod
    else:
        branches = [branching_factor(path[i]) for i in range(moves)]
        sum_path = sum(branches)
        log_prod = sum(math.log(max(1, b)) for b in branches)
        points = 100.0 * moves + 5.0 * sum_path + log_prod
    return {
        "moves": moves,
        "possibilities": br_start,
        "sum_branch_path": sum_path,
        "log_mult": log_prod,
        "points": round(points, 4),
    }


def build_real_levels(total_levels=300):
    seen_global = {}
    for inv_idx, inv in enumerate(TOTAL_INVENTORIES, start=1):
        goals = collect_goal_states_for_inventory(inv)
        print(
            f"Analyse BFS inventaire {inv_idx}/{len(TOTAL_INVENTORIES)} — "
            f"{len(goals)} etats but echantillonnes, coups jusqu'au 9 en bas...",
            flush=True,
        )
        for g_idx, goal_state in enumerate(goals, start=1):
            silent = g_idx > 1
            label = f"inventaire {inv_idx}/{len(TOTAL_INVENTORIES)} but {g_idx}/{len(goals)}"
            dist_map = distance_map_to_nearest_9_bottom(goal_state, label, silent=silent)
            for state, d in dist_map.items():
                key = canonical_mirror_state(state)
                item = (state, d)
                if key not in seen_global or merge_candidate_key(item) < merge_candidate_key(seen_global[key]):
                    seen_global[key] = item
    merged_states = list(seen_global.values())
    print("Calcul des scores (chemin optimal, points)...", flush=True)
    enriched = []
    for state, d_ref in merged_states:
        m = compute_position_metrics(state)
        if m is None:
            continue
        if m["moves"] != d_ref:
            continue
        enriched.append(
            {
                "state": state,
                "dist_ref": d_ref,
                **m,
            }
        )
    enriched.sort(
        key=lambda x: (
            x["points"],
            x["moves"],
            x["possibilities"],
            -x["log_mult"],
        )
    )
    if len(enriched) < total_levels:
        raise RuntimeError(
            f"Pool insuffisant pour {total_levels} niveaux sans doublons : "
            f"{len(enriched)} grilles uniques apres fusion (augmenter "
            f"GOAL_STATES_PER_MASK_CAP / GOAL_STATES_TOTAL_CAP_PER_INVENTORY ou baisser TOTAL_LEVELS)."
        )

    used = set()
    levels = []

    def pick_candidate(level_id):
        """Assouplit les contraintes si le pool est trop petit (peu d'etats avec 5+ coups, etc.)."""
        need_m = min_moves_required_for_level(level_id)
        bmin, bmax = branch_bounds_for_level(level_id)
        need_pts = min_points_required_for_level(level_id)

        # Niveau 1 : exactement 3 coups (meilleur score parmi les grilles a 3 coups optimaux).
        if level_id == 1:
            pool3 = [c for c in enriched if c["state"] not in used and c["moves"] == 3]
            if pool3:
                pool3.sort(
                    key=lambda x: (-x["points"], -x["possibilities"], -x["log_mult"])
                )
                return pool3[0]

        def attempt(cands, m_eff, p_lo, p_hi, pts_eff):
            for cand in cands:
                st = cand["state"]
                if st in used:
                    continue
                if cand["moves"] < m_eff:
                    continue
                p = cand["possibilities"]
                if p < p_lo or p > p_hi:
                    continue
                if cand["points"] < pts_eff:
                    continue
                return cand
            return None

        # 1) strict (meme plage de branches que la regle du niveau)
        ch = attempt(enriched, need_m, bmin, bmax, need_pts)
        if ch is not None:
            return ch
        # 2) plage 1..7 (le plateau a au plus 7 directions)
        ch = attempt(enriched, need_m, 1, 7, need_pts)
        if ch is not None:
            return ch
        # 3) baisser le nombre de coups exige (pool BFS limite)
        for slack in range(1, max(need_m, 20)):
            m_eff = max(1, need_m - slack)
            ch = attempt(enriched, m_eff, 1, 7, need_pts)
            if ch is not None:
                return ch
        # 4) baisser le plancher de points
        for pt_slack in range(1, 400):
            pts_eff = max(0.0, need_pts - 30 * pt_slack)
            for slack in range(0, max(need_m + 15, 25)):
                m_eff = max(1, need_m - slack)
                ch = attempt(enriched, m_eff, 1, 7, pts_eff)
                if ch is not None:
                    return ch
        # 5) tout puzzle encore autorise
        return attempt(enriched, 0, 1, 7, -1e9)

    for level_id in range(1, total_levels + 1):
        need_m = min_moves_required_for_level(level_id)
        bmin, bmax = branch_bounds_for_level(level_id)
        need_pts = min_points_required_for_level(level_id)
        chosen = pick_candidate(level_id)
        if chosen is None:
            raise RuntimeError(
                f"Pas de candidat pour le niveau {level_id} "
                f"(coups>={need_m}, possibilites dans [{bmin},{bmax}], points>={need_pts})."
            )
        used.add(chosen["state"])
        levels.append({
            "id": level_id,
            "name": f"Niveau {level_id}",
            "board": state_to_board(chosen["state"]),
            "moves": chosen["moves"],
            "min_required_moves": need_m,
            "min_required_branch": bmin,
            "max_required_branch": bmax,
            "min_required_points": need_pts,
            "possibilities": chosen["possibilities"],
            "sum_branch_path": chosen["sum_branch_path"],
            "log_mult": round(chosen["log_mult"], 6),
            "points": chosen["points"],
            "inventory": None,
            "score": difficulty_score(chosen["state"]),
            "piece_ids": state_to_piece_ids_board(chosen["state"]),
        })
    return levels

def config_signature():
    return {
        "target_empty": TARGET_EMPTY,
        "inventories": TOTAL_INVENTORIES,
        "level_min_moves_rule": "niveau 1 -> 3 coups ; niveau 2+ -> 5 + level_id // 20",
        "level_branch_rule": "cible 10..45 + id//25 projetee sur 1..7 (max du jeu)",
        "level_min_points_rule": "30 * (level_id // 10)",
        "level_assignment": "glouton par score puis assouplissement ; plusieurs etats but par inventaire ; pas de doublon de grille",
        "goal_sampling": f"jusqu'a {GOAL_STATES_TOTAL_CAP_PER_INVENTORY} pavages par inventaire (max {GOAL_STATES_PER_MASK_CAP} par position du but)",
        "scoring": "points = 100*moves + 5*sum(branching sur chemin) + sum(log(branching))",
        "total_levels": TOTAL_LEVELS,
        "max_bfs_states_per_inventory": MAX_BFS_STATES_PER_INVENTORY,
        "generator_version": LEVELS_GENERATOR_VERSION,
    }

def _env_truthy(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "oui")

def load_or_create_levels():
    signature = config_signature()
    force_rebuild = _env_truthy("PAPA_FORCE_REBUILD_LEVELS")
    if force_rebuild and os.path.exists(LEVELS_FILE):
        print(
            "PAPA_FORCE_REBUILD_LEVELS active : regeneration des niveaux (BFS)...",
            flush=True,
        )
    if not force_rebuild and os.path.exists(LEVELS_FILE):
        with open(LEVELS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("signature") == signature and data.get("levels"):
            n = len(data["levels"])
            print(
                f"Niveaux charges depuis {LEVELS_FILE!r} ({n} niveaux) — pas de recalcul BFS.",
                flush=True,
            )
            print(
                "Pour recalculer : supprimez levels.json ou lancez "
                "PAPA_FORCE_REBUILD_LEVELS=1 python3 papa_cursor_app_v2.py",
                flush=True,
            )
            return data["levels"]
    print("Creation des niveaux (analyse BFS complete), merci de patienter...", flush=True)
    levels = build_real_levels(TOTAL_LEVELS)
    with open(LEVELS_FILE, "w", encoding="utf-8") as f:
        json.dump({"signature": signature, "levels": levels}, f, ensure_ascii=False, indent=2)
    return levels

LEVELS = load_or_create_levels()

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        completed = set(int(x) for x in data.get("completed", []))
    else:
        completed = set()
    progress = {"completed": completed}
    recalc_unlocks(progress)
    return progress

def save_progress(progress):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump({"completed": sorted(progress["completed"])}, f, ensure_ascii=False, indent=2)

def recalc_unlocks(progress):
    total = len(LEVELS)
    unlocked = min(VISIBLE_START, total)
    def all_done(upto):
        upto = min(upto, total)
        return all(i in progress["completed"] for i in range(1, upto + 1))
    if all_done(20):
        unlocked = min(VISIBLE_AFTER_20, total)
    if all_done(50):
        unlocked = min(VISIBLE_AFTER_50, total)
    if all_done(100):
        unlocked = min(VISIBLE_AFTER_100, total)
    progress["unlocked"] = unlocked

PROGRESS = load_progress()

def esc(s):
    return html.escape(str(s), quote=True)


def parse_celebrate_query(query_string):
    """Retourne 20, 50, 100 ou 300 si ?celebrate= est valide, sinon None."""
    if not query_string:
        return None
    q = parse_qs(query_string)
    if "celebrate" not in q:
        return None
    try:
        v = int(q["celebrate"][0])
    except (ValueError, IndexError):
        return None
    return v if v in CELEBRATE_MILESTONES else None


def celebration_block(milestone):
    """HTML + JS : bandeau + feux d'artifice (canvas)."""
    labels = {
        20: "20 premiers niveaux",
        50: "50 niveaux",
        100: "100 niveaux",
        300: "300 niveaux",
    }
    lab = esc(labels.get(milestone, str(milestone)))
    return f"""
<div id="celebrate-banner" role="status" style="position:fixed;top:0;left:0;right:0;z-index:10001;text-align:center;padding:14px 16px;background:linear-gradient(90deg,#2f6fed,#6eb0f5);color:#fff;font-weight:700;box-shadow:0 4px 24px rgba(0,0,0,.18);font-size:clamp(14px,3.5vw,17px);animation:celebBanner 0.45s ease;">
  Bravo ! Palier : {lab} termines !
</div>
<style>
@keyframes celebBanner {{ from {{ opacity:0; transform:translateY(-14px);}} to {{ opacity:1; transform:none;}} }}
</style>
<canvas id="celebrate-canvas" aria-hidden="true" style="position:fixed;inset:0;pointer-events:none;z-index:10000;"></canvas>
<script>
(function() {{
  try {{
    if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {{
      var cv = document.getElementById('celebrate-canvas');
      if (cv) cv.remove();
      setTimeout(function() {{
        var b = document.getElementById('celebrate-banner');
        if (b) b.style.opacity = '0';
        setTimeout(function() {{ if (b) b.remove(); }}, 400);
      }}, 2200);
      try {{ history.replaceState({{}}, '', location.pathname); }} catch (e2) {{}}
      return;
    }}
  }} catch (e) {{}}
  var c = document.getElementById('celebrate-canvas');
  if (!c) return;
  var ctx = c.getContext('2d');
  function fit() {{ c.width = innerWidth; c.height = innerHeight; }}
  fit();
  window.addEventListener('resize', fit);
  var parts = [];
  var hues = [45, 200, 280, 125, 330];
  function burst(cx, cy, hue) {{
    for (var i = 0; i < 72; i++) {{
      var ang = Math.random() * Math.PI * 2;
      var sp = 2.2 + Math.random() * 6;
      parts.push({{ x: cx, y: cy, vx: Math.cos(ang) * sp, vy: Math.sin(ang) * sp - 1.2,
        g: 0.12 + Math.random() * 0.06, life: 1, col: 'hsl(' + hue + ',92%,62%)' }});
    }}
  }}
  var bursts = 6;
  for (var k = 0; k < bursts; k++) {{
    (function(ki) {{
      setTimeout(function() {{
        burst(0.15 * c.width + Math.random() * 0.7 * c.width, 0.2 * c.height + Math.random() * 0.45 * c.height, hues[ki % hues.length]);
      }}, ki * 320);
    }})(k);
  }}
  var tStart = performance.now();
  function frame(now) {{
    ctx.clearRect(0, 0, c.width, c.height);
    for (var i = parts.length - 1; i >= 0; i--) {{
      var p = parts[i];
      p.vy += p.g; p.x += p.vx; p.y += p.vy; p.life -= 0.011;
      if (p.life <= 0) {{ parts.splice(i, 1); continue; }}
      ctx.globalAlpha = Math.min(1, p.life);
      ctx.fillStyle = p.col;
      ctx.beginPath();
      ctx.arc(p.x, p.y, 2.4, 0, Math.PI * 2);
      ctx.fill();
    }}
    ctx.globalAlpha = 1;
    var elapsed = now - tStart;
    if (elapsed < 5200 || parts.length > 0) requestAnimationFrame(frame);
    else {{ c.remove(); window.removeEventListener('resize', fit); }}
  }}
  requestAnimationFrame(frame);
  var ban = document.getElementById('celebrate-banner');
  setTimeout(function() {{ if (ban) {{ ban.style.transition = 'opacity .5s'; ban.style.opacity = '0'; }} }}, 4200);
  setTimeout(function() {{ if (ban) ban.remove(); }}, 4800);
  try {{ history.replaceState({{}}, '', location.pathname); }} catch (e3) {{}}
}})();
</script>
"""


def page_template(title, body, celebrate=None):
    celeb = celebration_block(celebrate) if celebrate is not None else ""
    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>
:root {{
  --card: #fffaf0;
  --ink: #2a2a2a;
  --muted: #6f6a60;
  --accent: #2f6fed;
  --line: #d8d0c2;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: linear-gradient(180deg, #faf7f0 0%, #f0eadf 100%);
  color: var(--ink);
}}
.wrap {{
  max-width: 1100px;
  margin: 0 auto;
  padding: 24px;
}}
.hero {{
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 24px;
  padding: 28px;
  box-shadow: 0 10px 30px rgba(0,0,0,.06);
}}
h1, h2 {{ margin: 0 0 14px; }}
p {{ margin: 0 0 12px; line-height: 1.5; }}
.small {{ color: var(--muted); font-size: 14px; }}
.topbar {{
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: center;
  margin-bottom: 18px;
  flex-wrap: wrap;
}}
.btn, button {{
  appearance: none;
  border: 1px solid var(--line);
  background: white;
  color: var(--ink);
  border-radius: 14px;
  padding: 10px 14px;
  font-weight: 600;
  cursor: pointer;
  text-decoration: none;
  display: inline-flex;
  align-items: center;
  gap: 8px;
}}
.btn.primary, button.primary {{
  background: var(--accent);
  color: white;
  border-color: var(--accent);
}}
.grid-levels {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
  gap: 12px;
  margin-top: 20px;
}}
.level-card {{
  background: white;
  border: 1px solid var(--line);
  border-radius: 18px;
  padding: 14px;
  text-align: center;
  text-decoration: none;
  color: var(--ink);
  box-shadow: 0 6px 16px rgba(0,0,0,.04);
}}
.level-card.done {{
  border-color: #7bc67b;
  background: #f4fff4;
}}
.layout {{
  display: grid;
  grid-template-columns: 1.25fr .9fr;
  gap: 18px;
  margin-top: 18px;
}}
@media (max-width: 900px) {{
  .layout {{ grid-template-columns: 1fr; }}
}}
.panel {{
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 24px;
  padding: 20px;
  box-shadow: 0 10px 30px rgba(0,0,0,.06);
}}
.board {{
  display: grid;
  grid-template-columns: repeat({W}, 54px);
  grid-template-rows: repeat({H}, 54px);
  gap: 2px;
  justify-content: center;
  margin-top: 12px;
}}
.cell {{
  width: 54px;
  height: 54px;
  border-radius: 10px;
  border: none;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  font-size: 22px;
}}
.legend {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(100px, 1fr));
  gap: 10px;
  margin-top: 14px;
}}
.legend-item {{
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 14px;
}}
.swatch {{
  width: 20px;
  height: 20px;
  border-radius: 8px;
  border: 1px solid rgba(0,0,0,.14);
}}
.actions {{
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 16px;
}}
hr {{
  border: 0;
  border-top: 1px solid var(--line);
  margin: 18px 0;
}}
code {{
  background: #f2eee5;
  border-radius: 8px;
  padding: 2px 6px;
}}
.level-num {{
  font-size: 28px;
  font-weight: 800;
  line-height: 1.1;
}}
.phone-hint {{
  word-break: break-all;
}}
@media (max-width: 640px) {{
  .wrap {{ padding: 12px 10px; }}
  .hero {{ padding: 16px; border-radius: 18px; }}
  .topbar {{
    flex-direction: column;
    align-items: stretch;
    gap: 10px;
  }}
  .actions {{ justify-content: center; }}
  .btn, button {{
    padding: 12px 16px;
    min-height: 44px;
    touch-action: manipulation;
    -webkit-tap-highlight-color: transparent;
  }}
  .grid-levels {{
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 8px;
  }}
  .level-card {{ padding: 10px 8px; border-radius: 14px; }}
  .level-num {{ font-size: 22px; }}
  .layout {{ gap: 12px; margin-top: 12px; }}
  .panel {{ padding: 14px; border-radius: 18px; }}
  .board {{
    grid-template-columns: repeat({W}, 40px) !important;
    grid-template-rows: repeat({H}, 40px) !important;
    gap: 4px;
  }}
  .cell {{
    width: 40px !important;
    height: 40px !important;
    font-size: 16px !important;
    border-radius: 10px;
  }}
  .legend {{ font-size: 13px; gap: 8px; }}
  h1 {{ font-size: 1.35rem; }}
  h2 {{ font-size: 1.1rem; }}
}}
@media (max-width: 380px) {{
  .board {{
    grid-template-columns: repeat({W}, 34px) !important;
    grid-template-rows: repeat({H}, 34px) !important;
  }}
  .cell {{
    width: 34px !important;
    height: 34px !important;
    font-size: 14px !important;
  }}
}}
</style>
</head>
<body>
<div class="wrap">
{body}
</div>
<script>
function copyLink() {{
  navigator.clipboard.writeText(window.location.href);
  alert("Lien copié.");
}}
</script>
{celeb}
</body>
</html>"""

def _cell_edge_borders(pids, board, x, y):
    """Bord epais entre deux pieces differentes ; trait fin entre cases d'une meme piece."""
    val = int(board[y][x])
    pid = pids[y][x]
    thin = "1px solid rgba(0,0,0,.1)"
    inner = "1px solid rgba(255,255,255,.28)"
    piece_edge = "3px solid rgba(44,40,36,.82)"
    edge_out = "2px solid rgba(70,64,58,.45)"

    def seg(ny, nx):
        if ny < 0 or ny >= H or nx < 0 or nx >= W:
            return edge_out if val else thin
        nv = int(board[ny][nx])
        npi = pids[ny][nx]
        if val == 0 and nv == 0:
            return thin
        if val == 0 or nv == 0:
            return piece_edge
        if npi != pid:
            return piece_edge
        return inner

    bt = seg(y - 1, x) if y > 0 else (edge_out if val else thin)
    bb = seg(y + 1, x) if y < H - 1 else (edge_out if val else thin)
    bl = seg(y, x - 1) if x > 0 else (edge_out if val else thin)
    br = seg(y, x + 1) if x < W - 1 else (edge_out if val else thin)
    return f"border-top:{bt};border-right:{br};border-bottom:{bb};border-left:{bl};"


def render_board(board, piece_ids=None):
    pids = piece_ids if piece_ids is not None else infer_piece_ids_from_board(board)
    cells = []
    for y in range(H):
        for x in range(W):
            val = int(board[y][x])
            color = PIECE_COLORS.get(val, "#ddd")
            label = "" if val == 0 else str(val)
            borders = _cell_edge_borders(pids, board, x, y)
            cells.append(
                f'<div class="cell cell-piece" style="background:{color};{borders}box-sizing:border-box;" '
                f'title="{esc(PIECE_NAMES.get(val, val))}">{label}</div>'
            )
    return '<div class="board">' + "".join(cells) + '</div>'

def render_legend():
    ids = [1, 2, 3, 4, 5, 6, 9]
    items = []
    for pid in ids:
        items.append(f'<div class="legend-item"><span class="swatch" style="background:{PIECE_COLORS[pid]}"></span>{esc(PIECE_NAMES[pid])}</div>')
    return (
        '<p class="small" style="margin-bottom:10px">Bord <strong>epais</strong> = entre deux pieces ; '
        "trait leger blanc = entre cases d'une <strong>meme</strong> piece.</p>"
        '<div class="legend">' + "".join(items) + "</div>"
    )

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def home_page(celebrate=None):
    recalc_unlocks(PROGRESS)
    completed_count = len(PROGRESS["completed"])
    unlocked = PROGRESS["unlocked"]
    local_ip = get_local_ip()
    cards = []
    for level_id in range(1, unlocked + 1):
        done = " done" if level_id in PROGRESS["completed"] else ""
        cards.append(
            f'<a class="level-card{done}" href="/level/{level_id}">'
            f'<div class="level-num">{level_id}</div>'
            f'<div class="small">{"Termine" if level_id in PROGRESS["completed"] else "Ouvrir"}</div>'
            f'</a>'
        )
    body = f"""
<div class="hero">
  <div class="topbar">
    <div>
      <h1>{esc(WELCOME_MESSAGE)}</h1>
      <p class="small">Choisis un niveau et avance a ton rythme.</p>
    </div>
    <div class="actions">
      <a class="btn primary" href="/level/1">Commencer</a>
      <form method="post" action="/reset-all" style="display:inline">
        <button type="submit">Rejouer depuis le debut</button>
      </form>
    </div>
  </div>
  <hr>
  <p><strong>Niveaux termines :</strong> {completed_count}</p>
  <p class="small phone-hint"><strong>Telephone :</strong> sur le meme Wi-Fi, ouvre <code>http://{local_ip}:{PORT}</code></p>
  <div class="grid-levels">
    {''.join(cards)}
  </div>
</div>
"""
    return page_template("Jeu pour Papa", body, celebrate=celebrate)

def level_page(level_id, celebrate=None):
    if level_id < 1 or level_id > len(LEVELS):
        return page_template("Introuvable", '<div class="hero"><h1>Niveau introuvable</h1><p><a class="btn" href="/">Retour</a></p></div>')
    recalc_unlocks(PROGRESS)
    if level_id > PROGRESS["unlocked"]:
        return page_template("Introuvable", '<div class="hero"><h1>Niveau introuvable</h1><p><a class="btn" href="/">Retour</a></p></div>')
    lvl = LEVELS[level_id - 1]
    done = level_id in PROGRESS["completed"]
    min_req_m = int(lvl.get("min_required_moves", min_moves_required_for_level(level_id)))
    fb_lo, fb_hi = branch_bounds_for_level(level_id)
    min_req_b = int(lvl.get("min_required_branch", fb_lo))
    max_req_b = int(lvl.get("max_required_branch", fb_hi))
    min_req_pts = float(lvl.get("min_required_points", min_points_required_for_level(level_id)))
    next_level = level_id + 1 if level_id < PROGRESS["unlocked"] else None
    next_link = f"/level/{next_level}" if next_level else "/"
    body = f"""
<div class="topbar">
  <a class="btn" href="/">← Retour aux niveaux</a>
  <div class="actions">
    <button onclick="copyLink()">Copier le lien</button>
    <a class="btn" href="/level/{level_id}">Rejouer ce niveau</a>
  </div>
</div>

<div class="layout">
  <div class="panel">
    <h1>{esc(lvl["name"])}</h1>
    <p class="small">Lien direct : <code>/level/{level_id}</code></p>
    <h2 class="small" style="margin-top:18px;font-size:16px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)">Depart</h2>
    {render_board(lvl["board"], lvl.get("piece_ids"))}
    <p class="small" style="margin-top:16px"><strong>Objectif :</strong> amener le bloc marque <strong>9</strong> (But) en bas a gauche — il doit toucher le bord gauche <em>et</em> le bord bas de la grille (comme un bloc 4x1 dans le coin). Les autres pieces peuvent etre n'importe ou.</p>
    {render_legend()}
  </div>

  <div class="panel">
    <h2>Ce niveau</h2>
    <p><strong>Numero :</strong> {level_id}</p>
    <p><strong>Etat :</strong> {"Termine" if done else "En cours"}</p>
    <p><strong>Paliers pour ce numero :</strong> coups &ge; {min_req_m}, possibilites au depart entre {min_req_b} et {max_req_b}, score &ge; {min_req_pts:g}</p>
    <p><strong>Coups pour cette position :</strong> {int(lvl.get("moves", 0))} &nbsp;|&nbsp; <strong>Possibilites (depart) :</strong> {int(lvl.get("possibilities", 0))}</p>
    <p><strong>Points (difficulte) :</strong> {lvl.get("points", "—")} &nbsp;|&nbsp; Somme branching sur le chemin : {lvl.get("sum_branch_path", "—")} &nbsp;|&nbsp; log(produit) : {lvl.get("log_mult", "—")}</p>
    <p class="small"><strong>Un coup</strong> = tu choisis une direction : <strong>toutes</strong> les pieces qui peuvent glisser dans ce sens avancent <strong>en meme temps</strong> (une case par micro-pas), jusqu&apos;a ce que plus rien ne bouge — tout cela compte pour <strong>un seul</strong> coup. Score = 100×coups + 5×somme des possibilites sur le chemin + log(produit). Paliers : coups / possibilites / score.</p>
    <p><strong>Score 48 - pieces :</strong> {int(lvl.get("score", 0))}</p>
    <div class="actions">
      <form method="post" action="/complete/{level_id}" style="display:inline">
        <button type="submit" class="primary">J'ai termine ce niveau</button>
      </form>
      <form method="post" action="/reset/{level_id}" style="display:inline">
        <button type="submit">Reinitialiser ce niveau</button>
      </form>
      <a class="btn" href="{next_link}">Niveau suivant</a>
    </div>
    <hr>
    <p class="small">La progression est sauvegardee automatiquement.</p>
  </div>
</div>
"""
    return page_template(lvl["name"], body, celebrate=celebrate)

class AppHandler(BaseHTTPRequestHandler):
    def _send_html(self, html_text, status=200):
        data = html_text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        celebrate = parse_celebrate_query(parsed.query)
        if path == "/":
            self._send_html(home_page(celebrate=celebrate))
            return
        m = re.fullmatch(r"/level/(\d+)", path)
        if m:
            self._send_html(level_page(int(m.group(1)), celebrate=celebrate))
            return
        self._send_html(page_template("Introuvable", '<div class="hero"><h1>Page introuvable</h1><p><a class="btn" href="/">Retour</a></p></div>'), status=404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        m = re.fullmatch(r"/complete/(\d+)", path)
        if m:
            level_id = int(m.group(1))
            if 1 <= level_id <= len(LEVELS):
                PROGRESS["completed"].add(level_id)
                recalc_unlocks(PROGRESS)
                save_progress(PROGRESS)
            next_level = level_id + 1 if level_id < PROGRESS["unlocked"] else None
            sfx = (
                f"?celebrate={level_id}"
                if level_id in CELEBRATE_MILESTONES
                else ""
            )
            if next_level is not None:
                self._redirect(f"/level/{next_level}{sfx}")
            else:
                self._redirect(f"/{sfx}" if sfx else "/")
            return
        m = re.fullmatch(r"/reset/(\d+)", path)
        if m:
            level_id = int(m.group(1))
            PROGRESS["completed"].discard(level_id)
            recalc_unlocks(PROGRESS)
            save_progress(PROGRESS)
            self._redirect(f"/level/{level_id}")
            return
        if path == "/reset-all":
            PROGRESS["completed"].clear()
            recalc_unlocks(PROGRESS)
            save_progress(PROGRESS)
            self._redirect("/")
            return
        self._redirect("/")

def main():
    print("Inventaires utilises :")
    for i, inv in enumerate(TOTAL_INVENTORIES, start=1):
        print(f"{i}. {inv}  aire={total_inventory_area(inv)}")
    local_ip = get_local_ip()
    print(f"Serveur lance sur http://127.0.0.1:{PORT}")
    print(f"Telephone sur le meme Wi-Fi : http://{local_ip}:{PORT}")
    print("Progression sauvegardee dans:", PROGRESS_FILE)
    print("Niveaux sauvegardes dans:", LEVELS_FILE)
    server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    server.serve_forever()

if __name__ == "__main__":
    main()
