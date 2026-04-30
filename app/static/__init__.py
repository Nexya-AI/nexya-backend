"""Assets statiques backend (watermark logo NEXYA, etc.).

Packagé pour garantir que les fichiers `.png` / `.svg` / etc. sont
inclus dans le build wheel / Docker image. Les services backend lisent
ces assets en runtime (pas servis au client).
"""
