import re
import os
from urllib.parse import urlsplit
from zipfile import ZipFile
from pathlib import Path
from datetime import datetime, timedelta

import scrapy
from scrapy.exceptions import CloseSpider
from scrapy.http import Request
import py7zr

from documentcloud.constants import SUPPORTED_EXTENSIONS

from ..items import DocumentItem


class CorseSpider(scrapy.Spider):

    name = "DREAL Corse Scraper"

    # allowed_domains = ["corse.developpement-durable.gouv.fr"]

    start_urls = ["https://www.corse.developpement-durable.gouv.fr/projets-r640.html"]

    upload_limit_attained = False

    start_time = datetime.now()

    def check_time_limit(self):
        """Closes the spider automatically if it reaches a duration of 5h45min"""
        """as GitHub's actions have a 6 hours limit."""

        if self.time_limit != 0:

            limit = self.time_limit * 60
            now = datetime.now()

            if timedelta.total_seconds(now - self.start_time) > limit:
                raise CloseSpider(
                    f"Closed due to time limit ({self.time_limit} minutes)"
                )

    def check_upload_limit(self):
        """Closes the spider if the upload limit is attained."""
        if self.upload_limit_attained:
            raise CloseSpider("Closed due to max documents limit.")

    def parse(self, response, page=1):
        """Parse the starting page"""

        year_sections = response.css("#contenu div.fr-card a.fr-card__link")

        for sec in year_sections:
            section_title = sec.css("::text").get().strip()
            section_url = sec.attrib["href"]

            if any([str(y) in section_title for y in self.target_years]):

                yield response.follow(
                    section_url,
                    callback=self.parse_year_page,
                )

        next_page_link = response.css(
            "#contenu .fr-pagination__list .fr-pagination__link--next[href]"
        )

        if next_page_link:

            next_page_url = next_page_link.attrib["href"]

            yield response.follow(
                next_page_url,
                callback=self.parse,
                cb_kwargs=dict(page=page + 1),
            )

    def parse_year_page(self, response):

        self.logger.info(f"Scraping {response.request.url}")

        # Get year from page title

        page_title = response.css("#contenu h1.titre-article::text").get()

        year_from_title = page_title.replace("Projets ", "")

        # Links are collected first, then yielded at the end of the function

        collected_file_links = []

        # Tables layout. e.g. https://www.corse.developpement-durable.gouv.fr/projets-2024-a2139.html
        project_rows = response.css("#contenu table.spip tr")[1:]

        for row in project_rows:

            cells = row.css("td")

            dossier_cell = cells[0]
            project_cell = cells[1]
            commune_cell = cells[2]
            instruction_cell = cells[3]

            project_name = "".join(project_cell.css("::text").getall())
            commune = commune_cell.css("::text").get().strip()

            dossier_link = dossier_cell.css(".fr-download__link")
            if dossier_link:

                dossier_title = dossier_link.css("::text").get().strip()
                dossier_url = dossier_link.attrib["href"]

                collected_file_links.append(
                    {
                        "title": dossier_title,
                        "url": dossier_url,
                        "project_name": project_name + " - " + commune,
                        "commune": commune,
                    }
                )

            decision_link = instruction_cell.css(".fr-download__link")
            if decision_link:
                decision_title = decision_link.css("::text").get().strip()
                decision_url = decision_link.attrib["href"]

                collected_file_links.append(
                    {
                        "title": decision_title,
                        "url": decision_url,
                        "project_name": project_name + " - " + commune,
                        "commune": commune,
                    }
                )

        if not project_rows:

            # Article layout e.g. https://www.corse.developpement-durable.gouv.fr/projets-2018-a1499.html

            file_card_links = response.css(
                "#contenu .fr-download--card .fr-download__link"
            )

            for fc_link in file_card_links:

                link_text = fc_link.css("::text").get().strip()
                link_url = fc_link.attrib["href"]

                project_name_paragraph = fc_link.xpath(
                    "./../../preceding-sibling::p[1]"
                )

                project_name_string_list = project_name_paragraph.css("::text").getall()

                try:
                    new_line_index = project_name_string_list.index("\n")

                    project_name_string_list = project_name_string_list[:new_line_index]
                except ValueError:
                    pass

                project_name_string_list = [
                    s for s in project_name_string_list if not s.startswith("\n")
                ]

                project_name = "".join(project_name_string_list)

                collected_file_links.append(
                    {
                        "title": link_text,
                        "url": link_url,
                        "project_name": project_name,
                    }
                )

        # Finished collecting, yield each found link

        for file_link in collected_file_links:

            doc_item = DocumentItem(
                title=file_link["title"],
                source_page_url=response.request.url,
                project=file_link["project_name"],
                year=year_from_title,
                authority="Préfecture de région Corse",
                category_local="Les décisions au cas par cas projets",
            )
            if "commune" in file_link:
                doc_item["commune_string"] = file_link["commune"]

            # Get source_file_url and check event_data
            source_file_url = response.urljoin(file_link["url"])
            doc_item["source_file_url"] = source_file_url

            already_processed = False
            if source_file_url.lower().endswith(".zip"):
                if source_file_url in self.event_data["zips"]:
                    already_processed = True

            elif source_file_url in self.event_data["documents"]:
                already_processed = True

            if not already_processed:

                self.logger.info(f"Processing {file_link['title']} {source_file_url}")

                yield response.follow(
                    file_link["url"],
                    method="HEAD",
                    callback=self.parse_document_headers,
                    cb_kwargs=dict(doc_item=doc_item),
                )

    def parse_document_headers(self, response, doc_item):

        self.check_time_limit()
        self.check_upload_limit()

        doc_item["publication_lastmodified"] = response.headers.get(
            "Last-Modified"
        ).decode("utf-8")

        # Detect zip files and process them separately
        if doc_item["source_file_url"].lower().endswith(".zip") or doc_item[
            "source_file_url"
        ].lower().endswith(".7z"):
            yield Request(
                url=response.request.url,
                callback=self.parse_zip_file,
                cb_kwargs=dict(doc_item=doc_item),
            )
        else:
            doc_item["file_from_zip"] = False
            yield doc_item

    def parse_zip_file(self, response, doc_item):

        self.check_time_limit()
        self.check_upload_limit()

        # Get the modification date of the zip in the headers
        publication_lastmodified = response.headers.get("Last-Modified").decode("utf-8")

        # Get the filename from the requested URL
        urlpath = urlsplit(response.request.url).path
        filename = os.path.basename(urlpath)

        # Create the folder to hold zip files if it does not exist yet
        if not os.path.exists("./downloaded_zips"):
            os.makedirs("./downloaded_zips")

        # Save the zip file in the folder
        with open(f"./downloaded_zips/{filename}", "wb") as file:
            file.write(response.body)

        # Create a folder to hold the extracted files
        extracted_files_folder = f"./downloaded_zips/{filename[:-4]}"
        if not os.path.exists(extracted_files_folder):
            os.makedirs(extracted_files_folder)

        # Open Zip file and extract files
        if doc_item["source_file_url"].lower().endswith(".zip"):
            with ZipFile(f"./downloaded_zips/{filename}", "r") as zip_file:
                zip_file.extractall(path=extracted_files_folder)
        elif doc_item["source_file_url"].lower().endswith(".7z"):
            with py7zr.SevenZipFile(f"./downloaded_zips/{filename}", "r") as archive:
                archive.extractall(path=extracted_files_folder)

        # Delete zip file
        os.remove(f"./downloaded_zips/{filename}")

        # List all files
        extracted_files_folder_path_obj = Path(extracted_files_folder)
        extracted_files_list = list(extracted_files_folder_path_obj.rglob("*"))

        # Unzip and delete nested zip files
        nested_zip_files = [
            f
            for f in extracted_files_list
            if f.is_file() and f.suffix.lower() in [".zip", ".7z"]
        ]

        while nested_zip_files:

            for nzf in nested_zip_files:
                # create a destination folder
                destination_folder = str(nzf)[:-4]
                if not os.path.exists(destination_folder):
                    os.makedirs(destination_folder)

                # unzip
                if str(nzf).lower().endswith(".zip"):
                    with ZipFile(str(nzf), "r") as zip_file:
                        zip_file.extractall(path=destination_folder)
                elif str(nzf).lower().endswith(".7z"):
                    with py7zr.SevenZipFile(str(nzf), "r") as archive:
                        archive.extractall(path=destination_folder)

                # delete original zip
                os.remove(str(nzf))

            # find zips again
            extracted_files_list = list(extracted_files_folder_path_obj.rglob("*"))
            nested_zip_files = [
                f
                for f in extracted_files_list
                if f.is_file() and f.suffix.lower() in [".zip", "7z"]
            ]

        # Make a list of seen files for event_data
        zip_seen_supported_files = []
        for f in extracted_files_list:
            if f.is_file():
                basename = os.path.basename(str(f))
                filename, file_ext = os.path.splitext(basename)
                if file_ext.lower() in SUPPORTED_EXTENSIONS:
                    relative_filepath = "/".join(str(f).split("/")[2:])
                    zip_seen_supported_files.append(relative_filepath)

        # Yield a document object for each file
        for f in extracted_files_list:
            if f.is_file():
                filepath = str(f)

                item_relative_filepath = os.path.join(*filepath.split(os.sep)[2:])

                event_data_path = (
                    doc_item["source_file_url"] + "/" + item_relative_filepath
                )

                basename = os.path.basename(str(f))
                filename, file_ext = os.path.splitext(basename)

                if file_ext.lower() in SUPPORTED_EXTENSIONS:

                    if not event_data_path in self.event_data["documents"]:

                        yield DocumentItem(
                            title=doc_item["title"],
                            project=doc_item["project"],
                            category_local=doc_item["category_local"],
                            authority=doc_item["authority"],
                            source_file_url=response.request.url,
                            source_filename=f.name,
                            source_page_url=doc_item["source_page_url"],
                            publication_lastmodified=publication_lastmodified,
                            local_file_path=str(f),
                            zip_seen_supported_files=zip_seen_supported_files,
                            file_from_zip=True,
                            year=doc_item["year"],
                            commune_string=doc_item["commune_string"],
                        )
                else:
                    self.logger.info(
                        f"Ignored {basename} (unsupported extension) from {response.request.url}"
                    )
