# CF Pozuelo Liga · BETA 1.0 · instalar en el iPhone

La app se publica gratis como página web con HTTPS (GitHub Pages) y se instala en el iPhone desde Safari: queda con su icono, a pantalla completa, y funciona sin conexión con los últimos datos. Un robot de GitHub lee la RFFM cada 10 minutos y actualiza los datos solo; el Mac no tiene que estar encendido.

## 1. Publicar (una sola vez, ~10 min, desde el ordenador)
1. Crea una cuenta en https://github.com (gratis) y pulsa **New repository**. Nombre, por ejemplo, `liga-pozuelo`. Déjalo **Public** (Pages gratis lo exige) y pulsa *Create*.
2. En el repositorio nuevo: **uploading an existing file** y arrastra **el contenido** de esta carpeta: `docs`, `extractor.py`, `requirements.txt` (no hace falta nada más). Pulsa *Commit changes*.
3. El robot: **Add file > Create new file**, en el nombre escribe exactamente `.github/workflows/actualizar.yml`, pega el contenido de `workflow_actualizar.yml.txt` y pulsa *Commit*.
4. **Settings > Pages**: *Source* = Deploy from a branch · Branch `main` · carpeta **/docs** · Save.
5. **Settings > Actions > General > Workflow permissions** = *Read and write permissions* · Save.
6. Pestaña **Actions > Actualizar datos de la liga > Run workflow** para la primera lectura. Debe salir en verde.
7. Tu dirección será `https://TU_USUARIO.github.io/liga-pozuelo/` (aparece en Settings > Pages tras 1–2 minutos).

## 2. Instalar en el iPhone 17 Pro
1. Abre esa dirección en **Safari** (tiene que ser Safari).
2. Botón **Compartir** > **Añadir a pantalla de inicio** > *Añadir*.
3. Ábrela desde el icono. Se actualiza sola al abrirla y cada 3 minutos mientras esté abierta.

## Nombres de las jugadoras: cifrados, solo los ves tú
Son menores y la dirección web es técnicamente pública. Por eso los nombres, dorsales, goles y tarjetas **se publican cifrados** (AES-256): quien abra el archivo solo ve texto ilegible. Tu iPhone los descifra con un código que tecleas una sola vez.
1. Inventa un código largo (una frase de 4-5 palabras sirve; **no** lo reutilices de otro sitio).
2. En GitHub: **Settings > Secrets and variables > Actions > New repository secret**, nombre `CLAVE_ACTAS`, valor tu código. Pulsa *Add secret*.
3. **Actions > Actualizar datos de la liga > Run workflow** (el robot cifrará las actas).
4. En el iPhone, pestaña **Jugadoras**: te pedirá el código. Escríbelo una vez; queda guardado en la app de ese iPhone.
Sin el secreto `CLAVE_ACTAS` las actas no se publican. Si cambias el código, el robot vuelve a cifrar y el iPhone te lo pedirá otra vez.

## Si algo falla
- **La pestaña Jugadoras sale vacía aun con el código:** el lector de actas aún no se ha comprobado con actas reales de la RFFM. Si no reconoce el formato, el robot publica `https://TU_USUARIO.github.io/liga-pozuelo/acta_esquema.txt` (estructura **sin nombres**). Cópiame su contenido y lo ajusto.
- **El robot sale en rojo y el log dice que la RFFM no responde/403:** puede bloquear los servidores de GitHub. Mientras tanto puedes actualizar desde tu Mac con `python3 extractor.py --salida docs/datos_liga.json` y subir el resultado (arrastrando la carpeta `docs`).
- GitHub pausa los robots programados tras 60 días sin actividad del repositorio: si deja de actualizar, pulsa *Run workflow*.
- Para actualizar la app a una versión nueva, sube de nuevo `docs` (y `extractor.py`) al repositorio.
