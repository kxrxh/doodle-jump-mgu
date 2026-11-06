"""Profile the same fresh-profile menu and tutorial workloads on the local host."""
import cProfile
import json
from pathlib import Path
import pstats
import tempfile
import time
from game import Game


def main():
    report={}
    with tempfile.TemporaryDirectory() as directory:
        game=Game(data=Path(directory))
        for name in ('startup','frames'):
            if name=='frames':
                game.step(touches=[(0,0,180,295)])
                game.step(touches=[(0,2,180,295)])
                for _ in range(120):game.step()
                assert game.jni.playing, 'Benchmark must enter the original tutorial'
            profiler=cProfile.Profile()
            start=time.perf_counter()
            profiler.enable()
            for _ in range(240):image=game.step()
            profiler.disable()
            seconds=time.perf_counter()-start
            assert image.size==(544,960)
            report[name]={'frames':240,'seconds':seconds,'mean_ms':seconds/240*1000}
            pstats.Stats(profiler).strip_dirs().sort_stats('cumtime').print_stats(12)
        output=Path(__file__).parent/'data'
        output.mkdir(exist_ok=True)
        image.save(output/'tutorial.png')
        (output/'benchmark.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report,indent=2))


if __name__=='__main__':
    main()
