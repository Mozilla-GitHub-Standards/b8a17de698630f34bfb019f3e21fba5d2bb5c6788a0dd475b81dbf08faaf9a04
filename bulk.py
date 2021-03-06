from parquet2bigquery.lib import bulk
import argparse


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("-b", "--bucket",
                        help="GCS Bucket",
                        action="store", required=True)

    parser.add_argument("-p", "--prefix",
                        help="Object Prefix",
                        action="store", required=True)

    parser.add_argument("-d", "--dataset",
                        help="BigQuery Destination Dataset",
                        action="store", required=False)

    parser.add_argument("-a", "--alias",
                        help="BigQuery Table Alias",
                        action="store", required=False)

    parser.add_argument("-c", "--concurrency",
                        help="Process concurrency",
                        default=10,
                        type=int,
                        action="store")

    glob_group = parser.add_mutually_exclusive_group()

    glob_group.add_argument("-g", "--glob-load",
                            dest='glob_load',
                            action="store_true")
    glob_group.add_argument("-G", "--no-glob-load",
                            dest='glob_load',
                            action="store_false")

    parser.set_defaults(glob_load=True)

    resume_group = parser.add_mutually_exclusive_group()

    resume_group.add_argument("-r", "--resume",
                              dest='resume_load',
                              action="store_true")
    resume_group.add_argument("-R", "--no-resume",
                              dest='resume_load',
                              action="store_false")

    parser.set_defaults(resume_load=True)

    args = parser.parse_args()

    bulk(args.bucket, args.prefix, args.concurrency, args.glob_load,
         args.resume_load, dest_dataset=args.dataset, alias=args.alias)


main()
