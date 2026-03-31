#!/usr/bin/env python3
"""
Build script para el monorepo de skills de Abs R&D.

Uso:
    python build.py                          # Empaqueta TODAS las skills
    python build.py postgresql-django        # Empaqueta una skill específica
    python build.py --list                   # Lista skills disponibles

Genera archivos .skill en el directorio releases/
"""

import sys
import zipfile
from pathlib import Path

SKILLS_DIR = Path(__file__).parent / "skills"
RELEASES_DIR = Path(__file__).parent / "releases"
EXCLUDE = {"__pycache__", ".DS_Store", "evals", "node_modules"}


def list_skills():
    """Lista todas las skills disponibles."""
    skills = sorted(
        d.name for d in SKILLS_DIR.iterdir()
        if d.is_dir() and (d / "SKILL.md").exists()
    )
    if not skills:
        print("No se encontraron skills en skills/")
        return []

    print(f"📋 Skills disponibles ({len(skills)}):\n")
    for name in skills:
        skill_md = SKILLS_DIR / name / "SKILL.md"
        # Extraer descripción del frontmatter
        desc = ""
        in_frontmatter = False
        for line in skill_md.read_text().splitlines():
            if line.strip() == "---":
                in_frontmatter = not in_frontmatter
                continue
            if in_frontmatter and line.strip().startswith("description:"):
                desc = line.split("description:", 1)[1].strip().lstrip(">").strip()
                break
        ref_count = len(list((SKILLS_DIR / name).rglob("references/*.md")))
        lines = sum(1 for _ in open(f) for f in (SKILLS_DIR / name).rglob("*.md"))
        print(f"  • {name}")
        if desc:
            print(f"    {desc[:80]}...")
        print(f"    {ref_count} referencias")
        print()
    return skills


def build_skill(skill_name):
    """Empaqueta una skill en un archivo .skill."""
    skill_dir = SKILLS_DIR / skill_name

    if not skill_dir.exists():
        print(f"❌ Skill no encontrada: {skill_name}")
        print(f"   Busca en: {SKILLS_DIR}")
        return False

    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        print(f"❌ SKILL.md no encontrado en {skill_dir}")
        return False

    RELEASES_DIR.mkdir(exist_ok=True)
    output = RELEASES_DIR / f"{skill_name}.skill"

    file_count = 0
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(skill_dir.rglob("*")):
            if not file_path.is_file():
                continue
            if any(part in EXCLUDE for part in file_path.parts):
                continue

            arcname = file_path.relative_to(SKILLS_DIR)
            zf.write(file_path, arcname)
            print(f"  ✓ {arcname}")
            file_count += 1

    size_kb = output.stat().st_size / 1024
    print(f"\n✅ {skill_name}.skill ({size_kb:.0f} KB, {file_count} archivos)")
    return True


def build_all():
    """Empaqueta todas las skills."""
    skills = sorted(
        d.name for d in SKILLS_DIR.iterdir()
        if d.is_dir() and (d / "SKILL.md").exists()
    )

    if not skills:
        print("❌ No se encontraron skills en skills/")
        return

    print(f"📦 Empaquetando {len(skills)} skill(s)...\n")
    results = []

    for name in skills:
        print(f"── {name} ──")
        success = build_skill(name)
        results.append((name, success))
        print()

    print("=" * 40)
    print("Resumen:")
    for name, success in results:
        status = "✅" if success else "❌"
        print(f"  {status} {name}")
    print(f"\nArchivos generados en: {RELEASES_DIR}/")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg == "--list":
            list_skills()
        elif arg == "--help":
            print(__doc__)
        else:
            print(f"📦 Empaquetando: {arg}\n")
            build_skill(arg)
    else:
        build_all()
