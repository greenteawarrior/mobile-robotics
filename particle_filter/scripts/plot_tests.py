import matplotlib.pyplot as plt 
import numpy as np 

xpos = np.array([1,2,2])
weights = np.array([10, 20, 30])

# xpos = np.zeros(len(self.particle_cloud))
# weights = np.zeros(len(self.particle_cloud))

# x_i = 0
# weights_i = 0

# for p in self.particle_cloud:
#     xpos[x_i] = p.x 
#     weights[weights_i] = p.w

#     x_i += 1
#     weights_i += 1

plt.xlabel('xpos')
plt.ylabel('weights')
plt.title('xpos vs weights')
plt.plot(xpos, weights)
plt.show(block=False)
print 'oh hi'

