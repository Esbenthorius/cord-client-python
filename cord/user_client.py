from __future__ import annotations

import base64
from pathlib import Path
from typing import List, Dict, Tuple

from cord.client import CordClient
from cord.configs import UserConfig
from cord.http.querier import Querier
from cord.http.utils import upload_to_signed_url_list
from cord.orm.dataset import SignedImagesURL, Image
from cord.orm.cloud_integration import CloudIntegration
from cord.orm.dataset import Dataset, DatasetType
from cord.orm.dataset import DatasetScope, DatasetAPIKey
from cord.orm.project import Project, ProjectImporter, ReviewMode
from cord.orm.project_api_key import ProjectAPIKey
from cord.utilities.client_utilities import APIKeyScopes


class CordUserClient:
    def __init__(self, user_config: UserConfig, querier: Querier):
        self.user_config = user_config
        self.querier = querier

    def create_private_dataset(self, dataset_title: str, dataset_type: DatasetType, dataset_description: str = None):
        return self.create_dataset(dataset_title, dataset_type, dataset_description)

    def create_dataset(self, dataset_title: str, dataset_type: DatasetType, dataset_description: str = None):
        dataset = {
            'title': dataset_title,
            'type': dataset_type,
        }

        if dataset_description:
            dataset['description'] = dataset_description

        return self.querier.basic_setter(Dataset, uid=None, payload=dataset)

    def create_dataset_api_key(
            self, dataset_hash: str, api_key_title: str, dataset_scopes: List[DatasetScope]) -> DatasetAPIKey:
        api_key_payload = {
            'dataset_hash': dataset_hash,
            'title': api_key_title,
            'scopes': list(map(lambda scope: scope.value, dataset_scopes))
        }
        response = self.querier.basic_setter(DatasetAPIKey, uid=None, payload=api_key_payload)
        return DatasetAPIKey.from_dict(response)

    def get_dataset_api_keys(self, dataset_hash: str) -> List[DatasetAPIKey]:
        api_key_payload = {
            'dataset_hash': dataset_hash,
        }
        api_keys: List[DatasetAPIKey] = self.querier.get_multiple(DatasetAPIKey, uid=None, payload=api_key_payload)
        return api_keys

    @staticmethod
    def create_with_ssh_private_key(ssh_private_key: str, password: str = None, **kwargs) -> CordUserClient:
        user_config = UserConfig.from_ssh_private_key(ssh_private_key, password, **kwargs)
        querier = Querier(user_config)

        return CordUserClient(user_config, querier)

    def create_project(self, project_title: str, dataset_hashes: List[str], project_description: str = "") -> str:
        project = {
            'title': project_title,
            'description': project_description,
            'dataset_hashes': dataset_hashes
        }

        return self.querier.basic_setter(Project, uid=None, payload=project)

    def create_project_api_key(self, project_hash: str, api_key_title: str, scopes: List[APIKeyScopes]) -> str:
        payload = {
            'title': api_key_title,
            'scopes': list(map(lambda scope: scope.value, scopes))
        }

        return self.querier.basic_setter(ProjectAPIKey, uid=project_hash, payload=payload)

    def get_project_api_keys(self, project_hash: str) -> List[ProjectAPIKey]:
        return self.querier.get_multiple(ProjectAPIKey, uid=project_hash)

    def create_project_from_cvat(self,
                                 cvat_directory_path: str,
                                 dataset_name: str,
                                 review_mode: ReviewMode = ReviewMode.LABELLED) \
            -> dict:
        """
        Export your CVAT project with the "CVAT for images 1.1" option and use this function to import
            your images and annotations into cord. Ensure that during you have the "Save images"
            checkbox enabled when exporting from CVAT.
        Args:
            cvat_directory_path:
                Path to the exported CVAT directory.
            dataset_name:
                The name of the dataset that will be created.
            review_mode:
                Set how much interaction is needed from the labeler and from the reviewer for the CVAT labels.
                    See the `ReviewMode` documentation for more details.

        Returns:
            A JSON `dict` representation of the ProjectImporter object. The `errors` list is a collection
            of warnings and errors which happened during the import of CVAT items into cord.

        Raises:
            ValueError:
                If the CVAT directory has an invalid format.
        """

        directory_path = Path(cvat_directory_path)
        image_path = directory_path.joinpath('images')
        image_default_path = image_path.joinpath('default')

        images = None
        if image_path in list(directory_path.iterdir()):
            if image_default_path in list(image_path.iterdir()):
                images = list(image_default_path.iterdir())

        if images is None:
            raise ValueError(f"No images found in `{image_default_path}`.")

        annotations_file_path = directory_path.joinpath("annotations.xml")
        if not annotations_file_path.is_file():
            raise ValueError(f"The file `{annotations_file_path}` does not exist.")

        dataset_hash, image_title_to_image_hash_map = self.__upload_cvat_images(images, dataset_name)

        with annotations_file_path.open('rb') as f:
            annotations_base64 = base64.b64encode(f.read()).decode('utf-8')

        payload = {
            "cvat": {
                "annotations_base64": annotations_base64,
            },
            "dataset_hash": dataset_hash,
            "image_title_to_image_hash_map": image_title_to_image_hash_map,
            "review_mode": ReviewMode.to_string(review_mode)
        }
        # We are currently returning the project_hash. We might want to expose something in the SDK
        # which gets the dataset_hash for a given project_hash.
        return self.querier.basic_setter(ProjectImporter, uid=None, payload=payload)

    def __upload_cvat_images(self,
                             images: List[Path],
                             dataset_name: str) -> Tuple[str, Dict[str, str]]:
        """
        This function does NOT create any image groups yet.
        Returns:
            * The created dataset_hash
            * A map from an image title to the image hash which is stored in the DB.
        """
        # TODO: once we support videos, need to make sure to import videos and images differently.

        short_names = list(map(lambda x: x.name, images))
        file_path_strings = list(map(lambda x: str(x), images))
        dataset = self.create_dataset(dataset_name, DatasetType.CORD_STORAGE)

        dataset_hash = dataset["dataset_hash"]

        dataset_api_key: DatasetAPIKey = self.create_dataset_api_key(
            dataset_hash,
            "CVAT Full Access API Key",
            [DatasetScope.READ, DatasetScope.WRITE])

        client = CordClient.initialise(
            dataset_hash,
            dataset_api_key.api_key,
            domain=self.user_config.domain,
        )

        signed_urls = client._querier.basic_getter(
            SignedImagesURL,
            uid=short_names
        )
        upload_to_signed_url_list(
            file_path_strings, signed_urls, client._querier, Image
        )

        image_title_to_image_hash_map = dict(map(lambda x: (x.title, x.data_hash), signed_urls))

        return dataset_hash, image_title_to_image_hash_map

    def get_cloud_integrations(self) -> List[CloudIntegration]:
        return self.querier.get_multiple(CloudIntegration)

