
from copy import copy
from datetime import datetime
import random

from loguru import logger
from pyzotero import zotero
from omegaconf import DictConfig, ListConfig
from openai import OpenAI
from tqdm import tqdm

from .utils import glob_match, send_email
from .retriever import get_retriever_cls
from .protocol import CorpusPaper
from .reranker import get_reranker_cls
from .construct_email import render_email



def normalize_path_patterns(patterns, config_key: str):
    if patterns is None:
        return None

    if not isinstance(patterns, (list, ListConfig)):
        full_config_key = config_key

        if not full_config_key.startswith("config."):
            full_config_key = f"config.zotero.{full_config_key}"

        raise TypeError(
            f"{full_config_key} must be a list of glob patterns or null, "
            'for example ["01-VLA&Embodied AI"].'
        )

    if any(not isinstance(pattern, str) for pattern in patterns):
        raise TypeError(
            f"{config_key} must contain only strings."
        )

    return list(patterns)



class Executor:
    def __init__(self, config: DictConfig):
        self.config = config

        self.include_path_patterns = normalize_path_patterns(
            config.zotero.include_path, "config.zotero.include_path"
        )
        self.ignore_path_patterns = normalize_path_patterns(
            config.zotero.ignore_path, "config.zotero.ignore_path"
        )

        # 两个独立研究方向的推荐配置
        self.tracks = config.executor.get("tracks", None)

        # 作者机构提取需要额外调用一次LLM，初始版本关闭以减少API限流
        self.generate_affiliations = bool(
            config.executor.get("generate_affiliations", True)
        )

        self.retrievers = {
            source: get_retriever_cls(source)(config)
            for source in config.executor.source
        }

        self.reranker = get_reranker_cls(
            config.executor.reranker
        )(config)

        self.openai_client = OpenAI(
            api_key=config.llm.api.key,
            base_url=config.llm.api.base_url,
        )

    def fetch_zotero_corpus(self) -> list[CorpusPaper]:
        logger.info("Fetching Zotero corpus")

        zot = zotero.Zotero(
            self.config.zotero.user_id,
            "user",
            self.config.zotero.api_key,
        )

        collections = zot.everything(zot.collections())
        collections = {c["key"]: c for c in collections}

        raw_corpus = zot.everything(
            zot.items(
                itemType="conferencePaper || journalArticle || preprint"
            )
        )

        raw_corpus = [
            item for item in raw_corpus
            if (item["data"].get("abstractNote") or "").strip()
        ]

        def get_collection_path(collection_key: str) -> str:
            collection = collections[collection_key]
            parent = collection["data"].get("parentCollection")
            name = collection["data"]["name"]

            if parent:
                return get_collection_path(parent) + "/" + name

            return name

        corpus = []

        for item in raw_corpus:
            data = item["data"]

            paths = [
                get_collection_path(key)
                for key in data.get("collections", [])
                if key in collections
            ]

            corpus.append(
                CorpusPaper(
                    title=data.get("title", ""),
                    abstract=data.get("abstractNote", ""),
                    added_date=datetime.strptime(
                        data["dateAdded"],
                        "%Y-%m-%dT%H:%M:%SZ",
                    ),
                    paths=paths,
                )
            )

        logger.info(
            f"Fetched {len(corpus)} Zotero papers with abstracts"
        )

        return corpus

    def filter_corpus(
        self,
        corpus: list[CorpusPaper],
    ) -> list[CorpusPaper]:
        """Apply the original global include_path and ignore_path filters."""

        if self.include_path_patterns:
            corpus = [
                paper
                for paper in corpus
                if any(
                    glob_match(path, pattern)
                    for path in paper.paths
                    for pattern in self.include_path_patterns
                )
            ]

        if self.ignore_path_patterns:
            corpus = [
                paper
                for paper in corpus
                if not any(
                    glob_match(path, pattern)
                    for path in paper.paths
                    for pattern in self.ignore_path_patterns
                )
            ]

        if (
            self.include_path_patterns
            or self.ignore_path_patterns
        ):
            samples = random.sample(
                corpus,
                min(5, len(corpus)),
            )

            sample_text = "\n".join(
                paper.title + " - " + ", ".join(paper.paths)
                for paper in samples
            )

            logger.info(
                f"Selected {len(corpus)} Zotero papers:\n{sample_text}"
            )

        return corpus

    def select_track_papers(
        self,
        candidates,
        corpus: list[CorpusPaper],
    ):
        """
        Rank the same arXiv candidates independently
        for each Zotero collection.
        """

        selected = []
        selected_urls = set()

        for index, track in enumerate(
            self.tracks,
            start=1,
        ):
            name = str(
                track.get("name", f"Track {index}")
            )

            limit = int(
                track.get("max_paper_num", 0)
            )

            patterns = normalize_path_patterns(
                track.get("include_path"),
                f"config.executor.tracks[{name}].include_path",
            )

            if not patterns:
                raise ValueError(
                    f"Track '{name}' must define include_path."
                )

            if limit <= 0:
                raise ValueError(
                    f"Track '{name}' must have max_paper_num > 0."
                )

            # 独立筛选当前方向的Zotero参考文献
            track_corpus = [
                paper
                for paper in corpus
                if any(
                    glob_match(path, pattern)
                    for path in paper.paths
                    for pattern in patterns
                )
            ]

            logger.info(
                f"Track '{name}': matched "
                f"{len(track_corpus)} Zotero papers "
                f"using collection path(s) {patterns}"
            )

            if not track_corpus:
                logger.warning(
                    f"Track '{name}' has no Zotero papers "
                    f"matching {patterns}; skipped."
                )
                continue

            # 每个方向独立计算相关性分数。
            # 使用副本，避免不同方向的score相互覆盖。
            ranked = self.reranker.rerank(
                [copy(paper) for paper in candidates],
                track_corpus,
            )

            track_selected = []

            for paper in ranked:
                url = getattr(paper, "url", None)

                # 避免两个方向重复推荐同一篇论文
                if url and url in selected_urls:
                    continue

                paper.track_name = name
                paper.track_rank = len(track_selected) + 1

                track_selected.append(paper)

                if url:
                    selected_urls.add(url)

                if len(track_selected) >= limit:
                    break

            logger.info(
                f"Track '{name}': selected "
                f"{len(track_selected)} / {limit} papers"
            )

            if len(track_selected) < limit:
                logger.warning(
                    f"Track '{name}' could not fill its quota. "
                    "Some candidates may overlap with an earlier track."
                )

            selected.extend(track_selected)

        return selected

    @staticmethod
    def deduplicate_candidates(papers):
        """Remove duplicate candidate papers by URL."""

        unique = []
        seen = set()

        for paper in papers:
            url = getattr(paper, "url", None)

            if url and url in seen:
                continue

            if url:
                seen.add(url)

            unique.append(paper)

        return unique

    def run(self):
        # 1. 获取Zotero全部参考文献
        corpus = self.filter_corpus(
            self.fetch_zotero_corpus()
        )

        if not corpus:
            logger.error(
                "No Zotero papers found. Check Zotero API access "
                "and collection settings."
            )
            return

        # 2. 获取arXiv候选论文
        all_papers = []

        for source, retriever in self.retrievers.items():
            logger.info(f"Retrieving {source} papers...")

            papers = retriever.retrieve_papers()

            logger.info(
                f"Retrieved {len(papers)} {source} papers"
            )

            all_papers.extend(papers)

        # 3. 候选论文去重
        all_papers = self.deduplicate_candidates(
            all_papers
        )

        logger.info(
            f"Total unique candidate papers: {len(all_papers)}"
        )

        if not all_papers:
            if not self.config.executor.send_empty:
                logger.info(
                    "No new papers found. No email will be sent."
                )
                return

            selected_papers = []

        elif self.tracks:
            # 4. 分方向独立排序
            selected_papers = self.select_track_papers(
                all_papers,
                corpus,
            )

        else:
            # 兼容原项目的单方向排序方式
            selected_papers = self.reranker.rerank(
                all_papers,
                corpus,
            )

            selected_papers = selected_papers[
                :int(self.config.executor.max_paper_num)
            ]

        if (
            not selected_papers
            and not self.config.executor.send_empty
        ):
            logger.info(
                "No papers selected. No email will be sent."
            )
            return

        # 5. 生成中文TLDR
        logger.info(
            f"Generating TLDR for "
            f"{len(selected_papers)} selected papers..."
        )

        for paper in tqdm(selected_papers):
            paper.generate_tldr(
                self.openai_client,
                self.config.llm,
            )

            if self.generate_affiliations:
                paper.generate_affiliations(
                    self.openai_client,
                    self.config.llm,
                )
            else:
                paper.affiliations = None

        # 6. 生成分方向邮件
        logger.info("Sending email...")

        email_content = render_email(
            selected_papers
        )

        send_email(
            self.config,
            email_content,
        )

        logger.info("Email sent successfully")
