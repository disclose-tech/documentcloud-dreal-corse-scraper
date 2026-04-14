# Item Pipelines

import datetime
import re
import os
import shutil
from urllib.parse import urlparse
import logging
import json
import hashlib
import sys

from itemadapter import ItemAdapter
from scrapy.exceptions import DropItem
from documentcloud.constants import SUPPORTED_EXTENSIONS


from .log import SilentDropItem

from .departments import communes_2A, communes_2B


class SpiderPipeline:
    """Base class for pipelines that need access to the spider instance.

    Provides from_crawler() to store spider as self.spider.
    Inherit from this class instead of defining from_crawler() in each pipeline.
    """

    @classmethod
    def from_crawler(cls, crawler):
        pipeline = cls()
        pipeline.spider = crawler.spider
        return pipeline


class ParseDatePipeline:
    """Parse dates from scraped data."""

    def process_item(self, item):
        """Parse date from the extracted string."""

        # Publication date
        publication_dt = datetime.datetime.strptime(
            item["publication_lastmodified"], "%a, %d %b %Y %H:%M:%S GMT"
        )

        item["publication_date"] = publication_dt.strftime("%Y-%m-%d")
        item["publication_time"] = publication_dt.strftime("%H:%M:%S UTC")
        item["publication_datetime"] = (
            item["publication_date"] + " " + item["publication_time"]
        )

        item["publication_datetime_dcformat"] = (
            publication_dt.isoformat(timespec="microseconds") + "Z"
        )

        return item


class CategoryPipeline:
    """Attribute the final category of the document."""

    def process_item(self, item):
        if "cas par cas" in item["category_local"].lower():
            item["category"] = "Cas par cas"

        return item


class SourceFilenamePipeline:
    """Adds the source_filename field based on source_file_url."""

    def process_item(self, item):

        adapter = ItemAdapter(item)

        if not adapter.get("source_filename"):

            path = urlparse(item["source_file_url"]).path

            item["source_filename"] = os.path.basename(path)

        return item


class BeautifyPipeline:
    def process_item(self, item):
        """Beautify & harmonize project & title names."""
        # # Project

        item["project"] = item["project"].strip()
        item["project"] = item["project"].replace(" ", " ").replace("’", "'")
        item["project"] = item["project"].rstrip(".,")

        # enlever "representé(e) par"
        if re.search(r",? +représentée?(?: +par)?", item["project"]):

            item["project"] = re.sub(
                r",? +représentée?(?: +par)? .*?( - |$)", r"\1", item["project"]
            )

        item["project"] = item["project"][0].capitalize() + item["project"][1:]

        # # Title

        if item["file_from_zip"]:

            file_title = " - ".join(
                # folder1/folder2/document.pdf => folder1 - folder2 - document
                [
                    x
                    # folders
                    for x in item["local_file_path"].split("/")[2:-1]
                    # filename without extension
                    + [os.path.splitext(os.path.basename(item["local_file_path"]))[0]]
                ]
            )
            item["title"] += " " + file_title

        item["title"] = item["title"].replace("_", " ")
        item["title"] = item["title"].rstrip(".,")
        item["title"] = item["title"].strip()
        item["title"] = item["title"][0].upper() + item["title"][1:]

        # Commune

        adapter = ItemAdapter(item)
        if adapter.get("commune_string"):
            item["commune_string"] = (
                item["commune_string"].replace(" ", " ").replace("’", "'")
            )
            item["commune_string"] = item["commune_string"].strip()

        return item


class UploadLimitPipeline(SpiderPipeline):
    """Sends the signal to close the spider once the upload limit is attained."""

    def open_spider(self):
        self.number_of_docs = 0

    def process_item(self, item):
        spider = self.spider

        self.number_of_docs += 1

        if spider.upload_limit == 0 or self.number_of_docs <= spider.upload_limit:
            return item
        else:
            spider.upload_limit_attained = True
            raise SilentDropItem("Upload limit exceeded.")


class TagDepartmentsPipeline(SpiderPipeline):

    def process_item(self, item):

        def has_commune_match(commune_from_list, commune_from_doc):

            if (
                commune_from_list == commune_from_doc.lower()
                or commune_from_list.replace(" ", "-") == commune_from_doc.lower()
                or commune_from_list.replace("-", " ") == commune_from_doc.lower()
            ):
                return True
            else:
                return False

        def extract_commune_str_from_project(project_name):

            match = re.search(
                r"sur(?: les? territoires? des?)?(?: la| les)? communes? (?:d'|de la |de l'|du |de )(.*?(?: et (.*?))?),",
                project_name,
            )

            if match:
                commune_string = match.group(1)
            else:
                commune_string = ""
                self.spider.logger.warning(
                    f'Could not find municipalities in project name "{project_name}"'
                )

            return commune_string

        def extract_communes_list_from_str(commune_string):
            if " et " in commune_string:
                communes = commune_string.split(" et ")
            elif " - " in commune_string:
                communes = commune_string.split(" - ")
            else:
                communes = [commune_string]

            communes = [c.strip() for c in communes]

            return communes

        adapter = ItemAdapter(item)
        if not adapter.get("commune_string"):
            item["commune_string"] = extract_commune_str_from_project(item["project"])

        item["communes"] = extract_communes_list_from_str(item["commune_string"])

        departments = []

        matched_communes = []
        for com in item["communes"]:
            for c2a in communes_2A:
                if has_commune_match(c2a, com):
                    departments.append("2A")
                    matched_communes.append(c2a)

            for c2b in communes_2B:
                if has_commune_match(c2b, com):
                    departments.append("2B")
                    matched_communes.append(c2b)

        if departments:
            item["departments"] = sorted(list(set(departments)))
            item["departments_sources"] = ["regex"]
        else:
            self.spider.logger.warning(
                f"Failed to detect departments. string = {item['commune_string']}, communes = {item['communes']}, project_name= {item['project']}"
            )

        return item


class ProjectIDPipeline:

    def process_item(self, item):

        project_name = item["project"]
        source_page_url = item["source_page_url"]
        string_to_hash = source_page_url + " " + project_name

        hash_object = hashlib.sha256(string_to_hash.encode())
        hex_dig = hash_object.hexdigest()

        item["project_id"] = hex_dig

        return item


class UploadPipeline(SpiderPipeline):
    """Upload document to DocumentCloud & store event data."""

    def open_spider(self):
        spider = self.spider

        documentcloud_logger = logging.getLogger("documentcloud")
        documentcloud_logger.setLevel(logging.WARNING)
        squarelet_logger = logging.getLogger("squarelet")
        squarelet_logger.setLevel(logging.WARNING)

        if not spider.dry_run:
            try:
                spider.logger.info("Loading event data from DocumentCloud...")
                spider.event_data = spider.load_event_data()

            except Exception as e:
                raise Exception("Error loading event data").with_traceback(
                    e.__traceback__
                )
                sys.exit(1)
        else:
            # Load from json if present
            try:
                with open("event_data.json", "r") as file:
                    spider.logger.info("Loading event data from local JSON file...")

                    data = json.load(file)

                    spider.event_data = {
                        "documents": data["documents"],
                        "zips": data["zips"],
                    }
            except:
                spider.event_data = None

        if spider.event_data:
            spider.logger.info(
                f"Loaded event data ({len(spider.event_data['documents'])} documents, {len(spider.event_data['zips'])} zip files)"
            )

        else:
            spider.logger.info("No event data was loaded.")
            spider.event_data = {"documents": {}, "zips": {}}

    def process_item(self, item):
        spider = self.spider

        filename, file_extension = os.path.splitext(item["source_filename"])
        file_extension = file_extension.lower()

        # File path and event_data_key
        if item["file_from_zip"]:
            file_path = item["local_file_path"]

            item["source_file_zip_path"] = os.path.join(
                *item["local_file_path"].split(os.sep)[2:]
            )

            item["event_data_key"] = (
                item["source_file_url"] + "/" + item["source_file_zip_path"]
            )
        else:
            file_path = item["source_file_url"]
            item["event_data_key"] = item["source_file_url"]

        data = {
            "authority": item["authority"],
            "category": item["category"],
            "category_local": item["category_local"],
            "event_data_key": item["event_data_key"],
            "source_scraper": "DREAL Corse Scraper",
            "source_scraper_year": str(item["year"]),
            "source_file_url": item["source_file_url"],
            "source_filename": item["source_filename"],
            "source_page_url": item["source_page_url"],
            "publication_date": item["publication_date"],
            "publication_time": item["publication_time"],
            "publication_datetime": item["publication_datetime"],
            "project_id": item["project_id"],
        }
        if item["file_from_zip"]:
            data["source_file_zip_path"] = item["source_file_zip_path"]

        adapter = ItemAdapter(item)
        if adapter.get("departments") and adapter.get("departments_sources"):
            data["departments"] = item["departments"]
            data["departments_sources"] = item["departments_sources"]

        try:
            if not spider.dry_run:
                spider.client.documents.upload(
                    file_path,
                    original_extension=file_extension.lstrip("."),
                    project=spider.target_project,
                    title=item["title"],
                    description=item["project"],
                    publish_at=item["publication_datetime_dcformat"],
                    source="www.corse.developpement-durable.gouv.fr",
                    language="fra",
                    access=spider.access_level,
                    data=data,
                )

        except Exception as e:
            raise Exception("Upload error").with_traceback(e.__traceback__)

        else:  # No upload error, add to event_data
            last_modified = datetime.datetime.strptime(
                item["publication_lastmodified"], "%a, %d %b %Y %H:%M:%S %Z"
            ).isoformat()
            now = datetime.datetime.now().isoformat(timespec="seconds")

            spider.event_data["documents"][item["event_data_key"]] = {
                "last_modified": last_modified,
                "last_seen": now,
                "target_year": item["year"],
            }
            # Zip files
            if item["file_from_zip"]:
                # Check whether all files of the zip are in event_data documents
                zip_fully_processed = True
                for seen_file_path in item["zip_seen_supported_files"]:
                    file_event_data_path = (
                        item["source_file_url"] + "/" + seen_file_path
                    )
                    if file_event_data_path not in spider.event_data["documents"]:
                        zip_fully_processed = False
                if zip_fully_processed:
                    spider.event_data["zips"][item["source_file_url"]] = {
                        "last_modified": last_modified,
                        "last_seen": now,
                        "target_year": item["year"],
                    }

            # Store event_data (# only from the web interface)
            if spider.run_id and not spider.dry_run:
                spider.store_event_data(spider.event_data)

        return item

    def close_spider(self):
        """Update event data when the spider closes."""

        spider = self.spider

        if not spider.dry_run and spider.run_id:
            if spider.event_data:
                spider.store_event_data(spider.event_data)
                spider.logger.info(
                    f"Uploaded event data ({len(spider.event_data['documents'])} documents, {len(spider.event_data['zips'])} zip files)"
                )
                # Upload the event_data to the DocumentCloud interface
                now = datetime.datetime.now()
                timestamp = now.strftime("%Y%m%d_%H%M")
                filename = f"event_data_DREAL_Corse_{timestamp}.json"

                if spider.upload_event_data:
                    with open(filename, "w+") as event_data_file:
                        json.dump(spider.event_data, event_data_file)
                        spider.upload_file(event_data_file)
                    spider.logger.info(
                        f"Uploaded event data to the Documentcloud interface."
                    )

            else:
                spider.logger.info("No event data to upload.")

        if not spider.run_id:
            if spider.event_data:
                with open("event_data.json", "w") as event_data_file:
                    json.dump(spider.event_data, event_data_file)
                    spider.logger.info(
                        f"Saved file event_data.json ({len(spider.event_data['documents'])} documents, {len(spider.event_data['zips'])} zip files)"
                    )
            else:
                spider.logger.info("No event data to write.")


class MailPipeline(SpiderPipeline):
    """Send scraping run report when the spider closes."""

    def open_spider(self):
        self.scraped_items = []

    def process_item(self, item):

        self.scraped_items.append(item)

        return item

    def close_spider(self):

        spider = self.spider

        def print_item(item):
            item_string = f"""
            title: {item["title"]}
            project: {item["project"]}
            authority: {item["authority"]}
            category: {item["category"]}
            category_local: {item["category_local"]}
            year: {item["year"]}
            publication_date: {item["publication_date"]}
            source_filename: {item["source_filename"]}
            source_file_url: {item["source_file_url"]}
            source_page_url: {item["source_page_url"]}
            event_data_key: {item["event_data_key"]}
            """

            return item_string

        if len(spider.target_years) == 1:
            year_range_str = str(spider.target_years[0])
        else:
            year_range_str = (
                f"{str(spider.target_years[0])}-{str(spider.target_years[-1])}"
            )

        subject = f"DREAL Corse Scraper {year_range_str} (New: {len(self.scraped_items)}) [{spider.run_name}]"

        content = f"SCRAPED ITEMS ({len(self.scraped_items)})\n\n" + "\n\n".join(
            [print_item(item) for item in self.scraped_items]
        )

        start_content = f"DREAL Corse Scraper Addon Run {spider.run_id}"

        content = "\n\n".join([start_content, content])

        if not spider.dry_run:
            spider.send_mail(subject, content)


class DeleteZipFilesPipeline:
    """Delete files from downloaded zips to save some disk space"""

    def process_item(self, item):
        if item["file_from_zip"]:
            if os.path.isfile(item["local_file_path"]):
                os.remove(item["local_file_path"])
        return item

    def close_spider(self):
        # Delete the downloaded_zips folder
        if os.path.isdir("downloaded_zips"):
            shutil.rmtree("downloaded_zips")
