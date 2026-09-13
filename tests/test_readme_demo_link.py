import re
from pathlib import Path


def test_product_demo_link_uses_the_deployed_player() -> None:
	readme = Path(__file__).parents[1] / "README.md"
	match = re.search(r"\[▶ 观看产品演示\]\(([^)]+)\)", readme.read_text(encoding="utf-8"))

	assert match is not None
	assert match.group(1) == "https://locip123.github.io/WebAgent/"


def test_product_demo_page_uses_the_committed_video() -> None:
	demo_page = Path(__file__).parents[1] / "demo" / "index.html"
	html = demo_page.read_text(encoding="utf-8")

	assert '<video controls preload="metadata" playsinline>' in html
	assert '<source src="demo.mp4" type="video/mp4">' in html


def test_product_demo_page_is_deployed_to_github_pages() -> None:
	workflow = Path(__file__).parents[1] / ".github" / "workflows" / "deploy-demo.yml"
	contents = workflow.read_text(encoding="utf-8")

	assert "cp demo/index.html _site/index.html" in contents
	assert "actions/upload-pages-artifact@v3" in contents
	assert "actions/deploy-pages@v4" in contents
