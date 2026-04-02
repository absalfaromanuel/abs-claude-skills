# Abs RD — Claude Skills

Colección de skills para Claude AI desarrolladas por [Abs Research and Development](https://github.com/absalfaromanuel).

Cada skill enseña a Claude las mejores prácticas para un stack tecnológico específico, con documentación verificada contra fuentes oficiales.

## Skills Disponibles

| Skill | Descripción | Referencias | Estado |
|---|---|---|---|
| [postgresql-django](skills/postgresql-django/) | PostgreSQL + Django para SaaS en producción | 9 | ✅ Estable |

> Las skills futuras se agregan en `skills/nombre-de-skill/` siguiendo la misma estructura.

## Instalación

### Claude.ai (interfaz web/móvil)

1. Descarga el archivo `.skill` desde [Releases](../../releases)
2. **Settings** → **Profile** → **Skills** → **Add Skill**

### Claude Code — Opción A: Plugin Marketplace (recomendada)

```bash
# 1. Agregar el marketplace (una sola vez)
/plugin marketplace add absalfaromanuel/abs-claude-skills

# 2. Instalar la skill
/plugin install postgresql-django@abs-skills

# 3. Listo — invocar con:
/postgresql-django
```

### Claude Code — Opción B: Instalación manual

```bash
# 1. Clonar el repo
git clone git@github.com:absalfaromanuel/abs-claude-skills.git

# 2. Copiar la skill al directorio de skills
mkdir -p ~/.claude/skills
cp -r abs-claude-skills/skills/postgresql-django ~/.claude/skills/

# 3. Invocar con:
/postgresql-django
```

### Claude Code — Opción C: A nivel de proyecto

```bash
# Copiar dentro de un proyecto específico
cd ~/Projects/mi-proyecto
mkdir -p .claude/skills
cp -r /ruta/abs-claude-skills/skills/postgresql-django .claude/skills/
```

## Desarrollo

### Estructura del repositorio

```
abs-claude-skills/
├── README.md                ← Este archivo
├── LICENSE
├── .gitignore
├── marketplace.json         ← Catálogo para Claude Code plugin marketplace
├── plugin.json              ← Manifiesto del plugin
├── build.py                 ← Empaqueta skills → archivos .skill
├── skills/                  ← Código fuente de cada skill
│   ├── postgresql-django/
│   │   ├── SKILL.md         ← Router principal de la skill
│   │   └── references/      ← Archivos de referencia por tema
│   │       ├── schema-design.md
│   │       ├── django-orm.md
│   │       └── ...
│   ├── otra-skill-futura/   ← Cada skill en su carpeta
│   │   ├── SKILL.md
│   │   └── references/
│   └── ...
└── releases/                ← Archivos .skill generados (para Claude.ai)
    └── postgresql-django.skill
```

### Crear una nueva skill

```bash
# 1. Crear la estructura
mkdir -p skills/mi-nueva-skill/references

# 2. Crear SKILL.md con frontmatter
cat > skills/mi-nueva-skill/SKILL.md << 'EOF'
---
name: mi-nueva-skill
description: >
  Descripción de cuándo debe activarse esta skill.
  Incluir keywords específicos para triggering.
---

# Mi Nueva Skill

Instrucciones principales aquí (máximo ~500 líneas).

## Referencias
| Archivo | Cuándo leerlo |
|---------|--------------|
| `references/tema-1.md` | Cuando el usuario pregunte sobre X |
| `references/tema-2.md` | Cuando el usuario pregunte sobre Y |
EOF

# 3. Crear archivos de referencia
# skills/mi-nueva-skill/references/tema-1.md
# skills/mi-nueva-skill/references/tema-2.md

# 4. Agregar al marketplace.json (sección "plugins")
# para que sea instalable vía /plugin install en Claude Code

# 5. Empaquetar (genera .skill para Claude.ai)
python build.py mi-nueva-skill

# 6. Commit y push
git add .
git commit -m "feat: nueva skill mi-nueva-skill"
git push
```

### Comandos del build script

```bash
python build.py                     # Empaqueta TODAS las skills
python build.py postgresql-django   # Empaqueta una skill específica
python build.py --list              # Lista skills disponibles
```

### Reglas para escribir skills

1. **SKILL.md < 500 líneas** — Es el router, no la documentación completa
2. **Referencias < 500 líneas cada una** — Si crece más, dividir en dos
3. **Cero redundancia** — Cada tema tiene UNA sola fuente de verdad
4. **Cross-references** — Cuando un tema toca otro archivo, apuntar con ruta relativa
5. **Description "pushy"** — Incluir muchos keywords para que Claude active la skill

## Licencia

MIT License — Abs Research and Development
