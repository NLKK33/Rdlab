"""Etape 3 : capture de l'ecran et detection des regions modifiees.

La naivete couteuse : capturer 1920x1080x4 octets = 8,3 Mio par image.
A 30 img/s, 250 Mio/s. Inenvisageable. Trois leviers, cumulables :

  1. Ne transmettre QUE ce qui a change (dirty rectangles).
     Un bureau bureautique change sur < 2 % de sa surface la plupart du
     temps. C'est de loin le gain le plus important, et il est gratuit
     en qualite (c'est du sans perte au niveau du decoupage).

  2. Compresser ce qui reste (JPEG ici, faute de codec video).

  3. Adapter debit et frequence au reseau (voir netadapt.py).

Comment on detecte les changements ici : decoupage en tuiles de 64 px,
empreinte CRC32 par tuile, comparaison avec l'image precedente.
  - CRC32 est rapide (instruction materielle) mais n'est pas une preuve
    d'egalite : une collision ferait "rater" une mise a jour. En
    pratique, sur 2^32, c'est negligeable pour un labo ; on force une
    image complete periodiquement (keyframe) pour resorber toute derive.

CE QUE FONT LES SYSTEMES PROFESSIONNELS, et que ce code ne fait pas :
  - ils ne comparent pas les pixels : ils demandent au compositeur du
    systeme la liste des rectangles sales (DXGI Desktop Duplication sur
    Windows, PipeWire/portal sur Wayland, CGDisplayStream sur macOS).
    Le cout de detection tombe alors a zero.
  - ils capturent en GPU et encodent en GPU (NVENC, QuickSync, AMF),
    sans jamais ramener l'image en memoire centrale.
  - ils distinguent le curseur du contenu (curseur envoye en "sprite"
    separe, dessine cote client : latence percue quasi nulle).
  - ils separent texte/interface (codec sans perte, type RLE/palette) et
    video/photo (codec avec perte) dans la meme image.
"""

import struct
import time
import zlib
from io import BytesIO

try:
    import mss
    import numpy as np
    from PIL import Image
    HAVE_CAPTURE = True
except ImportError:                                   # pragma: no cover
    HAVE_CAPTURE = False

TILE = 64
_FRAME_HDR = struct.Struct("!IHBB")     # seq, n_tiles, quality, flags
_TILE_HDR = struct.Struct("!HHHHI")     # x, y, w, h, taille_jpeg

FLAG_KEYFRAME = 0x01


class ScreenSource:
    """Capture un moniteur et rend la liste des tuiles modifiees."""

    def __init__(self, monitor=1, tile=TILE, keyframe_interval=120):
        if not HAVE_CAPTURE:
            raise RuntimeError(
                "dependances manquantes : pip install mss pillow numpy")
        self._sct = mss.mss()
        mons = self._sct.monitors
        if monitor >= len(mons):
            raise ValueError("moniteur %d inexistant (%d disponibles)"
                             % (monitor, len(mons) - 1))
        self._mon = mons[monitor]
        self.width = self._mon["width"]
        self.height = self._mon["height"]
        self.tile = tile
        self.cols = (self.width + tile - 1) // tile
        self.rows = (self.height + tile - 1) // tile
        self._hashes = None
        self._seq = 0
        self._since_keyframe = 0
        self.keyframe_interval = keyframe_interval

    def grab(self):
        """-> tableau numpy RGB (H, W, 3)."""
        shot = self._sct.grab(self._mon)
        arr = np.frombuffer(shot.rgb, dtype=np.uint8)
        return arr.reshape(shot.height, shot.width, 3)

    def _tile_hashes(self, frame):
        """Une empreinte par tuile. Vectorise pour rester utilisable."""
        h = np.empty((self.rows, self.cols), dtype=np.uint32)
        for ty in range(self.rows):
            y0 = ty * self.tile
            y1 = min(y0 + self.tile, self.height)
            row = frame[y0:y1]
            for tx in range(self.cols):
                x0 = tx * self.tile
                x1 = min(x0 + self.tile, self.width)
                h[ty, tx] = zlib.crc32(np.ascontiguousarray(row[:, x0:x1]))
        return h

    def next_frame(self, quality=70, force_keyframe=False):
        """Retourne (payload_binaire, nb_tuiles, octets) ou None si rien
        n'a change. `None` est le cas NORMAL sur un bureau au repos."""
        frame = self.grab()
        hashes = self._tile_hashes(frame)

        keyframe = (force_keyframe or self._hashes is None
                    or self._since_keyframe >= self.keyframe_interval)
        if keyframe:
            dirty = [(ty, tx) for ty in range(self.rows)
                     for tx in range(self.cols)]
        else:
            changed = np.argwhere(hashes != self._hashes)
            dirty = [(int(ty), int(tx)) for ty, tx in changed]

        self._hashes = hashes
        if not dirty:
            # Rien n'a change : on n'envoie rien ET on ne fait PAS avancer
            # le compteur d'images cles. Le compteur doit mesurer des
            # images EMISES, pas des sondages : sinon un bureau totalement
            # immobile declencherait une image cle complete (ici 940 Kio)
            # toutes les keyframe_interval boucles de capture -- soit
            # toutes les ~10 s a 12 img/s, pour zero changement a l'ecran.
            # Une image cle sert a resorber une derive due a une collision
            # CRC32 ; or une collision ne peut survenir que si le contenu
            # a change. Un ecran fige n'en a donc aucun besoin.
            return None

        self._since_keyframe = 0 if keyframe else self._since_keyframe + 1

        chunks = []
        for ty, tx in dirty:
            y0, x0 = ty * self.tile, tx * self.tile
            y1 = min(y0 + self.tile, self.height)
            x1 = min(x0 + self.tile, self.width)
            buf = BytesIO()
            Image.fromarray(frame[y0:y1, x0:x1]).save(
                buf, format="JPEG", quality=quality, subsampling=2)
            data = buf.getvalue()
            chunks.append(_TILE_HDR.pack(x0, y0, x1 - x0, y1 - y0, len(data)))
            chunks.append(data)

        self._seq += 1
        header = _FRAME_HDR.pack(self._seq, len(dirty), quality,
                                 FLAG_KEYFRAME if keyframe else 0)
        payload = header + b"".join(chunks)
        return payload, len(dirty), len(payload)

    def close(self):
        self._sct.close()


def parse_frame(payload):
    """Cote client : payload binaire -> (seq, quality, keyframe, tuiles)."""
    seq, n_tiles, quality, flags = _FRAME_HDR.unpack_from(payload, 0)
    off = _FRAME_HDR.size
    tiles = []
    for _ in range(n_tiles):
        x, y, w, h, size = _TILE_HDR.unpack_from(payload, off)
        off += _TILE_HDR.size
        tiles.append((x, y, w, h, payload[off:off + size]))
        off += size
    return seq, quality, bool(flags & FLAG_KEYFRAME), tiles
