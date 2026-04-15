# QDArchive
Qualitative Data Analysis(QDA)helps you synthesize and structure information from unstructured data Through qualitative coding(labeling)of data, theoretical constructs are represented as codes in a code system which is hierarchically structured. QDArchive is a web service to publish and archive qualitative data With an emphasis on QDA files.


# QDA Pipeline

## Purpose
Search repositories for QDA files and store structured metadata.

## Run
python main.py

## Test
python test/test_pipeline.py

## Database
SQLite with 5 tables:
- PROJECTS
- FILES
- KEYWORDS
- PERSON_ROLE
- LICENSES

## Supported formats
.qdpx, .qda, .qdp, .mx22, .csv, etc.

## Notes
- Some repositories require authentication
- CSV detection assumes qualitative structure
