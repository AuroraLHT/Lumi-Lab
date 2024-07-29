import h5py
import argparse

def main(args):
    with open(args.path, "rb") as f:
        h5f = h5py.File(f)
        for key in h5f:
            print("-"*80)
            print(f"dataset [{key}]")
            print("Shape:", h5f[key].shape, "dtype:", h5f[key].dtype, "MBs:", round( h5f[key].nbytes / 1024 / 1024, 2) )
            print("Keys", list(h5f[key].attrs.keys()))

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path")

    args = parser.parse_args()
    main(args)