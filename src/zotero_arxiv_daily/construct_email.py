
from html import escape
import math

from .protocol import Paper


framework = """
<!DOCTYPE HTML>
<html>
<head>
  <style>
    .star-wrapper {
      font-size: 1.3em;
      line-height: 1;
      display: inline-flex;
      align-items: center;
    }
    .half-star {
      display: inline-block;
      width: 0.5em;
      overflow: hidden;
      white-space: nowrap;
      vertical-align: middle;
    }
    .full-star {
      vertical-align: middle;
    }
  </style>
</head>
<body>
<div>
    __CONTENT__
</div>

<br><br>
<div>
To unsubscribe, remove your email in your Github Action setting.
</div>

</body>
</html>
"""


def get_empty_html():
    block_template = """
    <table border="0" cellpadding="0" cellspacing="0" width="100%"
           style="font-family: Arial, sans-serif; border: 1px solid #ddd;
                  border-radius: 8px; padding: 16px; background-color: #f9f9f9;">
      <tr>
        <td style="font-size: 20px; font-weight: bold; color: #333;">
          No Papers Today. Take a Rest!
        </td>
      </tr>
    </table>
    """
    return block_template


def get_block_html(
    title: str,
    authors: str,
    rate: str,
    tldr: str,
    pdf_url: str,
    affiliations: str = None,
):
    block_template = """
    <table border="0" cellpadding="0" cellspacing="0" width="100%"
           style="font-family: Arial, sans-serif; border: 1px solid #ddd;
                  border-radius: 8px; padding: 16px; background-color: #f9f9f9;">
      <tr>
        <td style="font-size: 20px; font-weight: bold; color: #333;">
          {title}
        </td>
      </tr>
      <tr>
        <td style="font-size: 14px; color: #666; padding: 8px 0;">
          {authors}<br><i>{affiliations}</i>
        </td>
      </tr>
      <tr>
        <td style="font-size: 14px; color: #333; padding: 8px 0;">
          <strong>Relevance:</strong> {rate}
        </td>
      </tr>
      <tr>
        <td style="font-size: 14px; color: #333; padding: 8px 0;">
          <strong>TLDR:</strong> {tldr}
        </td>
      </tr>
      <tr>
        <td style="padding: 8px 0;">
          <a href="{pdf_url}"
             style="display: inline-block; text-decoration: none;
                    font-size: 14px; font-weight: bold; color: #fff;
                    background-color: #d9534f; padding: 8px 16px;
                    border-radius: 4px;">PDF</a>
        </td>
      </tr>
    </table>
    """

    return block_template.format(
        title=title,
        authors=authors,
        rate=rate,
        tldr=tldr,
        pdf_url=pdf_url,
        affiliations=affiliations,
    )


def get_stars(score: float):
    full_star = '<span class="full-star">⭐</span>'
    half_star = '<span class="half-star">⭐</span>'
    low, high = 6, 8

    if score <= low:
        return ""

    if score >= high:
        return full_star * 5

    interval = (high - low) / 10
    star_num = math.ceil((score - low) / interval)
    full_star_num = int(star_num / 2)
    half_star_num = star_num - full_star_num * 2

    return (
        '<div class="star-wrapper">'
        + full_star * full_star_num
        + half_star * half_star_num
        + "</div>"
    )


def render_email(papers: list[Paper]) -> str:
    if not papers:
        return framework.replace(
            "__CONTENT__",
            get_empty_html(),
        )

    # 统计每个研究方向实际选出的论文数量
    track_counts = {}

    for paper in papers:
        name = getattr(paper, "track_name", None)

        if name:
            track_counts[name] = (
                track_counts.get(name, 0) + 1
            )

    parts = []
    current_track = None
    track_rank = 0

    for paper in papers:
        track_name = getattr(
            paper,
            "track_name",
            None,
        )

        # 方向发生变化时，插入新的邮件板块标题
        if track_name and track_name != current_track:
            current_track = track_name
            track_rank = 0

            count = track_counts.get(
                track_name,
                0,
            )

            parts.append(
                '<h2 style="font-family: Arial, sans-serif; '
                'font-size: 22px; color: #243b53; '
                'margin: 24px 0 12px; padding-bottom: 8px; '
                'border-bottom: 2px solid #d9e2ec;">'
                f"{escape(str(track_name))}（{count}篇）</h2>"
            )

        track_rank += 1

        if track_name:
            title = f"{track_rank}. {paper.title}"
        else:
            title = paper.title

        rate = (
            round(paper.score, 1)
            if paper.score is not None
            else "Unknown"
        )

        author_list = list(paper.authors or [])

        if len(author_list) <= 5:
            authors = ", ".join(author_list)
        else:
            authors = ", ".join(
                author_list[:3]
                + ["..."]
                + author_list[-2:]
            )

        if paper.affiliations is not None:
            affiliations = ", ".join(
                paper.affiliations[:5]
            )

            if len(paper.affiliations) > 5:
                affiliations += ", ..."
        else:
            affiliations = "Unknown Affiliation"

        parts.append(
            get_block_html(
                title,
                authors,
                rate,
                paper.tldr or paper.abstract or "",
                paper.pdf_url or paper.url,
                affiliations,
            )
        )

    content = '<br>' + '</br><br>'.join(parts) + '</br>'

    return framework.replace(
        "__CONTENT__",
        content,
    )
