"""Etape 8 : adaptation au reseau.

Le probleme central du bureau a distance n'est pas "compresser bien",
c'est "ne jamais prendre de retard". Si l'encodeur produit plus vite que
le reseau ne transporte, la file d'emission grossit, et la latence
percue explose : l'utilisateur clique, et voit le resultat 3 secondes
plus tard parce qu'il regarde des images vieilles de 3 secondes. Ce
phenomene s'appelle le bufferbloat applicatif.

Regle d'or : en bureau a distance, JETER une image est toujours preferable
a la mettre en file d'attente. Une image perdue est invisible (la
suivante arrive 30 ms plus tard) ; une image en retard est visible et
degrade toute la session.

Ce controleur ajuste deux leviers a partir du RTT et du debit observe :
  - la qualite JPEG (20..85) : agit sur la taille de chaque image ;
  - la frequence cible (2..24 img/s) : agit sur le nombre d'images.

Reduire la qualite avant la frequence : l'oeil tolere mieux du flou que
des saccades quand on deplace une fenetre. Quand meme ca ne suffit pas,
on baisse la frequence.

DANS UN VRAI PRODUIT : le controle de congestion est bien plus fin
(GCC/BBR comme dans WebRTC), il estime la bande passante disponible a
partir du delai d'arrivee inter-paquets, et pilote le debit *cible* de
l'encodeur video, qui garantit un debit moyen. Ici on fait du "best
effort" comprehensible en 60 lignes.
"""

import time


class QualityController:
    MIN_Q, MAX_Q = 20, 85
    MIN_FPS, MAX_FPS = 2.0, 24.0

    def __init__(self, quality=70, fps=12.0):
        self.quality = quality
        self.fps = fps
        self.rtt_ms = 0.0
        self._rtt_ema = None
        self.bytes_sent = 0
        self._window_start = time.monotonic()
        self._window_bytes = 0
        self.throughput_kbps = 0.0
        self.dropped = 0

    # -- mesures ------------------------------------------------------
    def observe_rtt(self, rtt_ms):
        """RTT mesure par PING/PONG, lisse en moyenne exponentielle."""
        self._rtt_ema = (rtt_ms if self._rtt_ema is None
                         else 0.8 * self._rtt_ema + 0.2 * rtt_ms)
        self.rtt_ms = self._rtt_ema

    def observe_sent(self, nbytes):
        self.bytes_sent += nbytes
        self._window_bytes += nbytes
        now = time.monotonic()
        dt = now - self._window_start
        if dt >= 1.0:
            self.throughput_kbps = (self._window_bytes * 8 / 1000.0) / dt
            self._window_bytes = 0
            self._window_start = now

    def observe_drop(self):
        self.dropped += 1

    # -- decision -----------------------------------------------------
    def adjust(self, backlog_frames):
        """`backlog_frames` : images encodees mais pas encore ecoulees.

        C'est le signal le plus honnete dont on dispose sans instrumenter
        la pile TCP : si la file grossit, le reseau ne suit pas."""
        if backlog_frames >= 3 or self.rtt_ms > 250:
            self.quality = max(self.MIN_Q, self.quality - 10)
            if self.quality == self.MIN_Q:
                self.fps = max(self.MIN_FPS, self.fps - 2.0)
        elif backlog_frames == 0 and self.rtt_ms < 80:
            if self.fps < self.MAX_FPS:
                self.fps = min(self.MAX_FPS, self.fps + 0.5)
            else:
                self.quality = min(self.MAX_Q, self.quality + 2)
        return self.quality, self.fps

    def apply_hint(self, hint):
        """Le client peut demander explicitement un compromis.

        L'hote reste seul maitre : le client *suggere*, il n'impose pas
        (un client hostile ne doit pas pouvoir saturer l'hote)."""
        if hint == "low":
            self.quality, self.fps = 35, 6.0
        elif hint == "balanced":
            self.quality, self.fps = 65, 12.0
        elif hint == "high":
            self.quality, self.fps = 85, 20.0

    def stats(self):
        return {"quality": self.quality, "fps": round(self.fps, 1),
                "rtt_ms": round(self.rtt_ms, 1),
                "kbps": round(self.throughput_kbps),
                "dropped": self.dropped}
